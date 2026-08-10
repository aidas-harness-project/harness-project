"""Pure aggregation over span shards -- no filesystem writes, no DAO.

`dao.py aggregate-trace` owns the governed write of `_timing_summary.json`
(locked, atomic, schema-validated); this module owns the arithmetic that
produces its contents. Splitting them keeps the interval math testable
without a case directory, and keeps the DAO from carrying another few hundred
lines of algorithm.

The one subtlety worth stating up front is why self-time uses an interval
UNION rather than a sum. A parent span that fans out to parallel children
does not have `duration == sum(child durations)`: four page workers running
concurrently inside a 10-second pool span can easily sum to 35 seconds of
child time. Subtracting that sum would produce a negative parent self-time
and a rollup that blames the wrong op. The union of the children's occupied
intervals is what the parent was actually not doing itself, so
`self_time = duration - |union(children) intersected with self|` is the only
form that stays correct under real parallelism -- which is the exact
condition this instrumentation exists to measure.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "0.1"

SLA_START_OP = "sla.phase1.start"
SLA_END_OP = "sla.phase1.end"


def read_shards(spans_dir: Path) -> tuple[list[dict], int, int]:
    """Read every .jsonl shard under spans_dir.

    Returns (spans, dropped_lines, shard_count). A line that will not parse,
    or that parses to something other than a span-shaped object, is DISCARDED
    and counted -- never repaired and never fatal. The normal cause is a torn
    final line from a process killed mid-write, and losing one observation has
    to stay cheaper than any machinery to protect one.
    """
    spans: list[dict] = []
    dropped = 0
    shards = sorted(spans_dir.glob("*.jsonl")) if spans_dir.exists() else []
    for shard in shards:
        try:
            text = shard.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                dropped += 1
                continue
            if not isinstance(record, dict) or "span_id" not in record:
                dropped += 1
                continue
            if not isinstance(record.get("duration_s"), (int, float)):
                dropped += 1
                continue
            spans.append(record)
    return spans, dropped, len(shards)


def _union_length(intervals: Iterable[tuple[float, float]]) -> float:
    """Total length covered by a set of possibly-overlapping intervals."""
    ordered = sorted((s, e) for s, e in intervals if e > s)
    if not ordered:
        return 0.0
    total = 0.0
    cur_start, cur_end = ordered[0]
    for start, end in ordered[1:]:
        if start > cur_end:
            total += cur_end - cur_start
            cur_start, cur_end = start, end
        else:
            cur_end = max(cur_end, end)
    total += cur_end - cur_start
    return total


def _clip(intervals: Iterable[tuple[float, float]], lo: float, hi: float):
    for start, end in intervals:
        s, e = max(start, lo), min(end, hi)
        if e > s:
            yield (s, e)


def compute_self_times(spans: list[dict]) -> dict[str, float]:
    """self_time per span_id.

    A span's own time is its duration minus the wall time its children
    occupied. Children are compared on `t_start_mono`, which is only
    meaningful within one process -- so a child recorded in a different
    process is ignored for this subtraction rather than mixed into an
    incomparable clock. Cross-process work still appears in the rollups
    through its own spans; it simply is not deducted from a parent it cannot
    be aligned with.
    """
    by_id = {s["span_id"]: s for s in spans}
    children: dict[str, list[dict]] = {}
    for span in spans:
        parent = span.get("parent_span_id")
        if parent is not None and parent in by_id:
            children.setdefault(parent, []).append(span)

    self_times: dict[str, float] = {}
    for span in spans:
        duration = float(span.get("duration_s") or 0.0)
        start = span.get("t_start_mono")
        kids = children.get(span["span_id"], [])
        if start is None or not kids:
            self_times[span["span_id"]] = max(duration, 0.0)
            continue
        intervals = []
        for kid in kids:
            if kid.get("pid") != span.get("pid"):
                continue
            kid_start = kid.get("t_start_mono")
            if kid_start is None:
                continue
            intervals.append((float(kid_start),
                              float(kid_start) + float(kid.get("duration_s") or 0.0)))
        covered = _union_length(_clip(intervals, float(start), float(start) + duration))
        self_times[span["span_id"]] = max(duration - covered, 0.0)
    return self_times


def compute_critical_path(spans: list[dict], self_times: dict[str, float]) -> list[dict]:
    """The root-to-leaf chain with the greatest accumulated self-time.

    Answers 'what would have to get faster for the run to get faster'. Uses a
    memoized walk rather than recursion so a deep chain cannot blow the stack,
    and a cycle (which a corrupted shard could in principle produce) is broken
    rather than followed forever.
    """
    by_id = {s["span_id"]: s for s in spans}
    children: dict[str, list[str]] = {}
    roots: list[str] = []
    for span in spans:
        parent = span.get("parent_span_id")
        if parent is not None and parent in by_id:
            children.setdefault(parent, []).append(span["span_id"])
        else:
            roots.append(span["span_id"])

    best_cost: dict[str, float] = {}
    best_next: dict[str, str | None] = {}

    def cost(node: str, seen: frozenset[str]) -> float:
        if node in best_cost:
            return best_cost[node]
        own = self_times.get(node, 0.0)
        chosen: str | None = None
        chosen_cost = 0.0
        for kid in children.get(node, []):
            if kid in seen:
                continue
            kid_cost = cost(kid, seen | {kid})
            if kid_cost > chosen_cost:
                chosen_cost, chosen = kid_cost, kid
        best_cost[node] = own + chosen_cost
        best_next[node] = chosen
        return best_cost[node]

    if not roots:
        return []
    top = max(roots, key=lambda r: cost(r, frozenset({r})))
    total = sum(self_times.values()) or 1.0

    chain: list[dict] = []
    node: str | None = top
    guard = 0
    while node is not None and guard < len(spans) + 1:
        span = by_id[node]
        chain.append({
            "op": span.get("op", "?"),
            "span_id": node,
            "category": span.get("category", "compute"),
            "self_time_s": round(self_times.get(node, 0.0), 6),
            "duration_s": round(float(span.get("duration_s") or 0.0), 6),
            "share_of_total": round(self_times.get(node, 0.0) / total, 6),
        })
        node = best_next.get(node)
        guard += 1
    return chain


def _rollup(spans: list[dict], self_times: dict[str, float], key: str) -> dict[str, dict]:
    total = sum(self_times.values()) or 1.0
    out: dict[str, dict] = {}
    for span in spans:
        name = str(span.get(key) or "?")
        entry = out.setdefault(name, {"self_time_s": 0.0, "duration_s": 0.0,
                                      "count": 0, "error_count": 0})
        entry["self_time_s"] += self_times.get(span["span_id"], 0.0)
        entry["duration_s"] += float(span.get("duration_s") or 0.0)
        entry["count"] += 1
        if span.get("status") == "error":
            entry["error_count"] += 1
    for entry in out.values():
        entry["share_of_total"] = round(entry["self_time_s"] / total, 6)
        entry["self_time_s"] = round(entry["self_time_s"], 6)
        entry["duration_s"] = round(entry["duration_s"], 6)
    return out


def _parse_wall(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def compute_sla(spans: list[dict]) -> tuple[float | None, float, float | None]:
    """(sla_wall_clock_s, human_wait_s, active_s).

    The two SLA markers are joined on their WALL anchors, not perf_counter:
    the pipeline spans several processes and monotonic clocks are not
    comparable across them. human_wait is a union of the human_wait spans
    inside the window -- a union so two gates open at once are not deducted
    twice, which would make active_s smaller than the work actually took.
    """
    starts = [w for w in (_parse_wall(s.get("t_start_wall")) for s in spans
                          if s.get("op") == SLA_START_OP) if w]
    ends = [w for w in (_parse_wall(s.get("t_start_wall")) for s in spans
                        if s.get("op") == SLA_END_OP) if w]
    if not starts or not ends:
        return None, _human_wait_all(spans), None
    start, end = min(starts), max(ends)
    wall = (end - start).total_seconds()
    if wall < 0:
        return None, _human_wait_all(spans), None

    intervals = []
    for span in spans:
        if span.get("category") != "human_wait":
            continue
        anchor = _parse_wall(span.get("t_start_wall"))
        if anchor is None:
            continue
        s = (anchor - start).total_seconds()
        e = s + float(span.get("duration_s") or 0.0)
        intervals.append((s, e))
    human = _union_length(_clip(intervals, 0.0, wall))
    return round(wall, 6), round(human, 6), round(max(wall - human, 0.0), 6)


def _human_wait_all(spans: list[dict]) -> float:
    intervals = []
    for span in spans:
        if span.get("category") != "human_wait":
            continue
        anchor = _parse_wall(span.get("t_start_wall"))
        if anchor is None:
            continue
        base = anchor.timestamp()
        intervals.append((base, base + float(span.get("duration_s") or 0.0)))
    return round(_union_length(intervals), 6)


def _pool_name(op: str) -> str:
    return op[len("pool."):] if op.startswith("pool.") else op


def summarize(spans: list[dict], *, case_id: str, run_id: str,
              generated_at: str, dropped_span_lines: int, shard_count: int,
              input_class: str | None = None,
              cold_or_warm: str | None = None) -> dict:
    """Build the full `_timing_summary.json` body."""
    self_times = compute_self_times(spans)
    wall, human, active = compute_sla(spans)

    worker_config: dict[str, int] = {}
    observed: dict[str, int] = {}
    cache_hits = cache_misses = cache_hit_spans = 0
    provider_calls = provider_errors = total_attempts = 0
    by_reason: dict[str, int] = {}
    acquire_count = total_poll = 0
    total_wait = max_wait = total_held = 0.0
    dropped_attrs = 0

    for span in spans:
        attrs = span.get("attrs") or {}
        dropped_attrs += int(span.get("dropped_attrs") or 0)
        op = str(span.get("op") or "")
        category = span.get("category")
        if span.get("status") == "cache_hit":
            cache_hit_spans += 1
        if op.startswith("pool."):
            name = _pool_name(op)
            if isinstance(attrs.get("worker_count"), int):
                worker_config[name] = max(worker_config.get(name, 0),
                                          attrs["worker_count"])
            if isinstance(attrs.get("observed_max_concurrency"), int):
                observed[name] = max(observed.get(name, 0),
                                     attrs["observed_max_concurrency"])
        if category == "compute":
            cache_hits += int(attrs.get("cache_hits") or 0)
            cache_misses += int(attrs.get("cache_misses") or 0)
        if category == "provider":
            provider_calls += 1
            if span.get("status") == "error":
                provider_errors += 1
            total_attempts += int(attrs.get("attempts") or 1)
            reason = attrs.get("retry_reason_code")
            if isinstance(reason, str):
                by_reason[reason] = by_reason.get(reason, 0) + 1
        if category == "lock":
            if op.endswith(".acquire"):
                acquire_count += 1
                wait_s = float(attrs.get("wait_s") or 0.0)
                total_wait += wait_s
                max_wait = max(max_wait, wait_s)
                total_poll += int(attrs.get("poll_count") or 0)
            if op.endswith(".hold"):
                total_held += float(attrs.get("held_s")
                                    or span.get("duration_s") or 0.0)

    summary = {
        "schema_version": SCHEMA_VERSION,
        "case_id": case_id,
        "run_id": run_id,
        "generated_at": generated_at,
        "span_count": len(spans),
        "shard_count": shard_count,
        "dropped_span_lines": dropped_span_lines,
        "sla_wall_clock_s": wall,
        "human_wait_s": human,
        "active_s": active,
        "critical_path": compute_critical_path(spans, self_times),
        "by_category": _rollup(spans, self_times, "category"),
        "by_op": _rollup(spans, self_times, "op"),
        "total_self_time_s": round(sum(self_times.values()), 6),
        "worker_config": worker_config,
        "observed_max_concurrency": observed,
        "cache_stats": {
            "cache_hits": cache_hits,
            "cache_misses": cache_misses,
            "cache_hit_spans": cache_hit_spans,
        },
        "retry_stats": {
            "provider_calls": provider_calls,
            "provider_errors": provider_errors,
            "total_attempts": total_attempts,
            "by_reason_code": by_reason,
        },
        "lock_stats": {
            "acquire_count": acquire_count,
            "total_wait_s": round(total_wait, 6),
            "max_wait_s": round(max_wait, 6),
            "total_poll_count": total_poll,
            "total_held_s": round(total_held, 6),
        },
        "dropped_attrs": dropped_attrs,
    }
    if input_class is not None:
        summary["input_class"] = input_class
    if cold_or_warm is not None:
        summary["cold_or_warm"] = cold_or_warm
    return summary
