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

SCHEMA_VERSION = "0.2"

SLA_START_OP = "sla.phase1.start"
SLA_END_OP = "sla.phase1.end"
STAGE_ATTEMPT_START_OP = "stage.attempt.start"
STAGE_ATTEMPT_END_OP = "stage.attempt.end"
STAGE_ATTEMPT_ABANDONED_OP = "stage.attempt.abandoned"
STAGE_ATTEMPT_OPS = frozenset({
    STAGE_ATTEMPT_START_OP,
    STAGE_ATTEMPT_END_OP,
    STAGE_ATTEMPT_ABANDONED_OP,
})


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


def _subtract_intervals(base: tuple[float, float], cuts: Iterable[tuple[float, float]]):
    """Return the portions of *base* not covered by the union of *cuts*."""
    remaining = [base]
    for cut_start, cut_end in sorted(cuts):
        next_remaining = []
        for start, end in remaining:
            if cut_end <= start or cut_start >= end:
                next_remaining.append((start, end))
                continue
            if cut_start > start:
                next_remaining.append((start, min(cut_start, end)))
            if cut_end < end:
                next_remaining.append((max(cut_end, start), end))
        remaining = next_remaining
    return [(start, end) for start, end in remaining if end > start]


def _wall_interval(span: dict) -> tuple[float, float] | None:
    anchor = _parse_wall(span.get("t_start_wall"))
    if anchor is None:
        return None
    start = anchor.timestamp()
    duration = float(span.get("duration_s") or 0.0)
    return start, start + max(duration, 0.0)


def _stage_marker_key(span: dict) -> tuple[str, int] | None:
    attrs = span.get("attrs") or {}
    stage = attrs.get("stage_name")
    attempt = span.get("attempt")
    if not isinstance(stage, str) or not stage or not isinstance(attempt, int) or attempt < 1:
        return None
    return stage, attempt


def _sla_window(spans: list[dict]) -> tuple[float, float] | None:
    starts = [w.timestamp() for w in (_parse_wall(s.get("t_start_wall")) for s in spans
              if s.get("op") == SLA_START_OP) if w]
    ends = [w.timestamp() for w in (_parse_wall(s.get("t_start_wall")) for s in spans
            if s.get("op") == SLA_END_OP) if w]
    if not starts or not ends:
        return None
    start, end = min(starts), max(ends)
    return (start, end) if end >= start else None


def summarize_stage_attempts(spans: list[dict],
                             run_state_stages: list[dict] | None = None):
    """Pair cross-process stage markers and build separate wall-time rollups.

    These derived intervals never enter ordinary span self-time. They answer
    which orchestration attempt occupied the wall clock; the unexplained
    remainder is deliberately named unattributed rather than model reasoning.
    """
    grouped: dict[tuple[str, int], dict[str, list[dict]]] = {}
    for span in spans:
        op = span.get("op")
        if op not in STAGE_ATTEMPT_OPS:
            continue
        key = _stage_marker_key(span)
        if key is None:
            continue
        bucket = grouped.setdefault(key, {"start": [], "end": [], "abandoned": []})
        if op == STAGE_ATTEMPT_START_OP:
            bucket["start"].append(span)
        elif op == STAGE_ATTEMPT_END_OP:
            bucket["end"].append(span)
        else:
            bucket["abandoned"].append(span)

    human_intervals = [interval for span in spans
                       if span.get("category") == "human_wait"
                       for interval in [_wall_interval(span)] if interval is not None]
    # `dispatch` is excluded from tool intervals on purpose: it CONTAINS the
    # subagent's own work, so folding it in would report the agent's thinking
    # time as observed tool time and drive unattributed_active_s to ~0 -- the
    # exact false precision the T13 naming exists to avoid. It is summarised
    # separately below, where the round trip stays distinguishable from the
    # work it wraps.
    tool_spans = [span for span in spans
                  if span.get("category") not in ("marker", "human_wait", "dispatch")
                  and span.get("op") not in STAGE_ATTEMPT_OPS
                  and _wall_interval(span) is not None]
    tool_intervals = [_wall_interval(span) for span in tool_spans]

    dispatch_spans = [span for span in spans
                      if span.get("category") == "dispatch"
                      and _wall_interval(span) is not None]

    # Spans that NAME their owning stage (trace.py resolves it from the marker
    # directory at write time). These are attributable even when stage windows
    # overlap, because ownership is recorded rather than inferred from a
    # timestamp. A span carrying `stage_candidates` instead names several and
    # is deliberately not claimed by any of them.
    owned_intervals: dict[tuple[str, int], list[tuple[float, float]]] = {}
    ambiguous_intervals: list[tuple[float, float]] = []
    unowned_intervals: list[tuple[float, float]] = []
    for span in tool_spans:
        interval = _wall_interval(span)
        stage_name = span.get("stage_name")
        stage_attempt = span.get("stage_attempt")
        if isinstance(stage_name, str) and isinstance(stage_attempt, int):
            owned_intervals.setdefault((stage_name, stage_attempt), []).append(interval)
        elif span.get("stage_candidates"):
            ambiguous_intervals.append(interval)
        else:
            unowned_intervals.append(interval)
    sla_window = _sla_window(spans)

    attempts: list[dict] = []
    valid_windows: dict[tuple[str, int], tuple[float, float]] = {}
    for (stage, attempt), markers in sorted(grouped.items()):
        starts, ends = markers["start"], markers["end"]
        pairing = "complete"
        start_wall = end_wall = None
        if len(starts) > 1 or len(ends) > 1:
            pairing = "duplicate_marker"
        elif not starts and ends:
            pairing = "orphan_end"
        elif starts and not ends:
            # An abandoned marker says the attempt demonstrably stopped (a
            # cascade invalidated it). That is not the same as an attempt still
            # running or lost to a crash: it has a known terminal fate and owes
            # no further marker. T13 keeps its duration null either way -- the
            # abandon time is when the cascade ran, not when work stopped -- so
            # this distinction is about coverage honesty, not about a duration.
            pairing = "abandoned" if markers["abandoned"] else "open"
        elif not starts and not ends:
            pairing = "abandoned" if markers["abandoned"] else "open"
        else:
            start_wall = _parse_wall(starts[0].get("t_start_wall"))
            end_wall = _parse_wall(ends[0].get("t_start_wall"))
            if start_wall is None or end_wall is None or end_wall < start_wall:
                pairing = "invalid_order"

        outcome = "open"
        if len(ends) == 1:
            raw_outcome = (ends[0].get("attrs") or {}).get("attempt_outcome")
            outcome = raw_outcome if isinstance(raw_outcome, str) and raw_outcome else "unknown"
        elif markers["abandoned"]:
            outcome = "interrupted"

        item = {
            "stage_name": stage,
            "attempt": attempt,
            "outcome": outcome,
            "pairing_status": pairing,
            "started_at": starts[0].get("t_start_wall") if len(starts) == 1 else None,
            "ended_at": ends[0].get("t_start_wall") if len(ends) == 1 else None,
            "raw_wall_s": None,
            "human_wait_s": 0.0,
            "active_wall_s": None,
            "observed_tool_overlap_s": None,
            "unattributed_active_s": None,
            "attribution_status": pairing if pairing != "complete" else "complete",
            "in_sla_window": False,
        }
        if pairing == "complete" and start_wall is not None and end_wall is not None:
            start_ts, end_ts = start_wall.timestamp(), end_wall.timestamp()
            valid_windows[(stage, attempt)] = (start_ts, end_ts)
            clipped_human = list(_clip(human_intervals, start_ts, end_ts))
            human_s = _union_length(clipped_human)
            raw_s = end_ts - start_ts
            active_parts = _subtract_intervals((start_ts, end_ts), clipped_human)
            owned = owned_intervals.get((stage, attempt), [])
            owned_parts, inferred_parts, ambiguous_parts = [], [], []
            for active_start, active_end in active_parts:
                owned_parts.extend(_clip(owned, active_start, active_end))
                inferred_parts.extend(
                    _clip(unowned_intervals, active_start, active_end))
                ambiguous_parts.extend(
                    _clip(ambiguous_intervals, active_start, active_end))
            owned_s = _union_length(owned_parts)
            observed_s = _union_length(owned_parts + inferred_parts)
            active_s = max(raw_s - human_s, 0.0)
            item.update({
                "raw_wall_s": round(raw_s, 6),
                "human_wait_s": round(human_s, 6),
                "active_wall_s": round(active_s, 6),
                "observed_tool_overlap_s": round(min(observed_s, active_s), 6),
                "unattributed_active_s": round(max(active_s - observed_s, 0.0), 6),
                "owned_tool_overlap_s": round(min(owned_s, active_s), 6),
                "ambiguous_tool_overlap_s": round(
                    min(_union_length(ambiguous_parts), active_s), 6),
                "in_sla_window": bool(sla_window and end_ts > sla_window[0]
                                      and start_ts < sla_window[1]),
            })
        attempts.append(item)

    # Where stage windows overlap, a TIMESTAMP cannot decide which stage a tool
    # span belongs to. Spans that name their own stage can: ownership was
    # recorded at write time, so overlap does not make them ambiguous.
    #
    # Two different verdicts follow. An attempt whose overlapping window
    # contains only owned spans is fully attributable and keeps its numbers. An
    # attempt that also overlaps spans nothing claims falls back to the
    # original fail-closed behaviour for the inferred part -- charging one
    # unowned span to both stages would double-count it.
    overlapping: set[tuple[str, int]] = set()
    valid_items = list(valid_windows.items())
    for idx, (left_key, (left_start, left_end)) in enumerate(valid_items):
        for right_key, (right_start, right_end) in valid_items[idx + 1:]:
            if min(left_end, right_end) > max(left_start, right_start):
                overlapping.update((left_key, right_key))
    for item in attempts:
        key = (item["stage_name"], item["attempt"])
        if key not in overlapping:
            continue
        window = valid_windows.get(key)
        contested = []
        if window is not None:
            contested = list(_clip(unowned_intervals + ambiguous_intervals,
                                   window[0], window[1]))
        if not contested:
            # Every tool span inside this window declared its owner.
            item["attribution_status"] = "complete_explicit_ownership"
            continue
        # Contested window: the inferred part cannot be split between stages,
        # so the blended figures fall back to null exactly as before. What was
        # measured explicitly is kept -- owned_tool_overlap_s remains a valid
        # lower bound on this attempt's tool time, and discarding it would
        # throw away the only unambiguous observation in the window.
        item["attribution_status"] = "overlapping_stage_attempts"
        item["observed_tool_overlap_s"] = None
        item["unattributed_active_s"] = None

    by_stage: dict[str, dict] = {}
    for state in run_state_stages or []:
        stage = state.get("stage_name")
        if isinstance(stage, str):
            by_stage.setdefault(stage, {
                "attempt_count_observed": 0, "closed_attempt_count": 0,
                "failed_attempt_count": 0, "open_attempt_count": 0,
                "abandoned_attempt_count": 0,
                "total_attempt_active_wall_s": 0.0,
                "passed_attempt_active_wall_s": 0.0, "human_wait_s": 0.0,
                "observed_tool_overlap_s": 0.0, "unattributed_active_s": 0.0,
                "attribution_complete": True,
                "skipped": state.get("status") == "skipped",
            })
    for item in attempts:
        stage = by_stage.setdefault(item["stage_name"], {
            "attempt_count_observed": 0, "closed_attempt_count": 0,
            "failed_attempt_count": 0, "open_attempt_count": 0,
            "abandoned_attempt_count": 0,
            "total_attempt_active_wall_s": 0.0,
            "passed_attempt_active_wall_s": 0.0, "human_wait_s": 0.0,
            "observed_tool_overlap_s": 0.0, "unattributed_active_s": 0.0,
            "attribution_complete": True, "skipped": False,
        })
        stage["attempt_count_observed"] += 1
        if item["pairing_status"] == "complete":
            stage["closed_attempt_count"] += 1
            active_s = float(item["active_wall_s"] or 0.0)
            stage["total_attempt_active_wall_s"] += active_s
            stage["human_wait_s"] += float(item["human_wait_s"] or 0.0)
            if item["outcome"] == "passed":
                stage["passed_attempt_active_wall_s"] += active_s
            else:
                stage["failed_attempt_count"] += 1
        elif item["pairing_status"] == "open":
            stage["open_attempt_count"] += 1
        elif item["pairing_status"] == "abandoned":
            stage["abandoned_attempt_count"] += 1
        if item["attribution_status"] not in (
                "complete", "complete_explicit_ownership"):
            stage["attribution_complete"] = False
        elif item["observed_tool_overlap_s"] is not None:
            stage["observed_tool_overlap_s"] += item["observed_tool_overlap_s"]
            stage["unattributed_active_s"] += item["unattributed_active_s"]

    for stage in by_stage.values():
        for key in ("total_attempt_active_wall_s", "passed_attempt_active_wall_s",
                    "human_wait_s"):
            stage[key] = round(stage[key], 6)
        if stage["attribution_complete"]:
            stage["observed_tool_overlap_s"] = round(stage["observed_tool_overlap_s"], 6)
            stage["unattributed_active_s"] = round(stage["unattributed_active_s"], 6)
        else:
            stage["observed_tool_overlap_s"] = None
            stage["unattributed_active_s"] = None

    expected_keys = set()
    for state in run_state_stages or []:
        if state.get("status") == "skipped":
            continue
        stage = state.get("stage_name")
        count = state.get("attempt_count")
        if isinstance(stage, str) and isinstance(count, int):
            # A non-skipped terminal/in-progress stage necessarily executed.
            # If incidental output writes advanced it with attempt_count=0,
            # it still owes one explicit marker pair; report the missing
            # coverage instead of treating zero as fully observed.
            expected = max(count, 1) if state.get("status") != "pending" else count
            expected_keys.update((stage, n) for n in range(1, expected + 1))
    observed_keys = set(grouped)
    missing = expected_keys - observed_keys
    pairing_counts = {name: sum(1 for item in attempts if item["pairing_status"] == name)
                      for name in ("complete", "open", "orphan_end",
                                   "duplicate_marker", "invalid_order",
                                   "abandoned")}
    # An abandoned attempt is ACCOUNTED FOR: a cascade ended it and recorded
    # that it did. Counting it as incomplete coverage would mean a run can
    # never report complete coverage merely because an invalidation happened,
    # which is a normal event. It still contributes no duration.
    # Per-stage dispatch accounting. Recorded dispatches split a stage's
    # residual into the round trip (structural, engineerable) and whatever else
    # sat inside the marker window (operator-side work, one person's habits).
    # A stage with no recorded dispatch keeps nulls rather than zeros: zero
    # would claim the round trip was measured and found to be nothing.
    #
    # Tokens follow the same rule for the same reason. They are the only
    # recorded figure that explains an agent stage's wall time -- tool spans
    # account for well under 2% of it -- so a stage with no recorded token
    # count must say "not recorded", not "0 tokens", which would read as a
    # stage that did no model work at all.
    for stage_name, entry in by_stage.items():
        entry.setdefault("dispatch_count", 0)
        entry.setdefault("dispatch_wall_s", None)
        entry.setdefault("dispatch_round_trip_s", None)
        entry.setdefault("dispatch_input_tokens", None)
        entry.setdefault("dispatch_output_tokens", None)
        entry.setdefault("dispatch_total_tokens", None)
        entry.setdefault("dispatch_tool_uses", None)
        entry.setdefault("dispatch_tokens_per_s", None)
    for span in dispatch_spans:
        stage_name = (span.get("attrs") or {}).get("stage_name")
        if not isinstance(stage_name, str):
            continue
        entry = by_stage.setdefault(stage_name, {
            "attempt_count_observed": 0, "closed_attempt_count": 0,
            "failed_attempt_count": 0, "open_attempt_count": 0,
            "abandoned_attempt_count": 0,
            "total_attempt_active_wall_s": 0.0,
            "passed_attempt_active_wall_s": 0.0, "human_wait_s": 0.0,
            "observed_tool_overlap_s": 0.0, "unattributed_active_s": 0.0,
            "attribution_complete": True, "skipped": False,
            "dispatch_count": 0, "dispatch_wall_s": None,
            "dispatch_round_trip_s": None,
            "dispatch_input_tokens": None, "dispatch_output_tokens": None,
            "dispatch_total_tokens": None, "dispatch_tool_uses": None,
            "dispatch_tokens_per_s": None,
        })
        duration = float(span.get("duration_s") or 0.0)
        attrs = span.get("attrs") or {}
        entry["dispatch_count"] += 1
        entry["dispatch_wall_s"] = round(
            (entry["dispatch_wall_s"] or 0.0) + duration, 6)
        reported = attrs.get("agent_reported_s")
        if isinstance(reported, (int, float)):
            entry["dispatch_round_trip_s"] = round(
                (entry["dispatch_round_trip_s"] or 0.0)
                + max(duration - float(reported), 0.0), 6)
        for attr_key, out_key in (
                ("input_tokens", "dispatch_input_tokens"),
                ("output_tokens", "dispatch_output_tokens"),
                ("total_tokens", "dispatch_total_tokens"),
                ("tool_uses", "dispatch_tool_uses")):
            value = attrs.get(attr_key)
            # bool is an int subclass and would silently sum as 0/1.
            if isinstance(value, int) and not isinstance(value, bool):
                entry[out_key] = (entry[out_key] or 0) + value

    # Rate is derived last, over the stage's summed dispatches, so a stage
    # dispatched twice reports one honest rate rather than two averaged. It is
    # tokens per second of DISPATCH wall, not of the stage's marker window: the
    # window can contain operator-side work no dispatch covers.
    for entry in by_stage.values():
        total = entry.get("dispatch_total_tokens")
        wall = entry.get("dispatch_wall_s")
        if isinstance(total, int) and isinstance(wall, (int, float)) and wall > 0:
            entry["dispatch_tokens_per_s"] = round(total / wall, 3)

    settled = {"complete", "abandoned"}
    coverage = {
        "run_state_attempts_expected": len(expected_keys),
        "trace_attempts_started": sum(len(v["start"]) for v in grouped.values()),
        "trace_attempts_closed": sum(len(v["end"]) for v in grouped.values()),
        "complete_attempts": pairing_counts["complete"],
        "open_attempts": pairing_counts["open"],
        "abandoned_attempts": pairing_counts["abandoned"],
        "orphan_end_attempts": pairing_counts["orphan_end"],
        "duplicate_marker_attempts": pairing_counts["duplicate_marker"],
        "invalid_order_attempts": pairing_counts["invalid_order"],
        "missing_marker_attempts": len(missing),
        "coverage_complete": (not missing and not (observed_keys - expected_keys)
                              and all(item["pairing_status"] in settled
                                      for item in attempts))
                             if run_state_stages is not None
                             else all(item["pairing_status"] in settled
                                      for item in attempts),
    }
    return attempts, by_stage, coverage


def _pool_name(op: str) -> str:
    return op[len("pool."):] if op.startswith("pool.") else op


def summarize(spans: list[dict], *, case_id: str, run_id: str,
              generated_at: str, dropped_span_lines: int, shard_count: int,
              input_class: str | None = None,
              cold_or_warm: str | None = None,
              run_state_stages: list[dict] | None = None) -> dict:
    """Build the full `_timing_summary.json` body."""
    work_spans = [s for s in spans if s.get("op") not in STAGE_ATTEMPT_OPS]
    self_times = compute_self_times(work_spans)
    wall, human, active = compute_sla(spans)
    stage_attempts, by_stage, stage_coverage = summarize_stage_attempts(
        spans, run_state_stages)
    closed_attempts = [item for item in stage_attempts
                       if item["pairing_status"] == "complete"]
    closed_raw_intervals = []
    for item in closed_attempts:
        start = _parse_wall(item["started_at"])
        end = _parse_wall(item["ended_at"])
        if start is not None and end is not None:
            closed_raw_intervals.append((start.timestamp(), end.timestamp()))
    closed_attempt_metrics = {
        # A sum is load/cost, never elapsed time when stages overlap.
        "closed_attempt_active_wall_sum_s": round(sum(
            float(item["active_wall_s"] or 0.0) for item in closed_attempts), 6),
        # This is intentionally raw wall time: active-time subtraction requires
        # clipping human intervals across the entire union, not summing each
        # attempt's subtraction independently.
        "closed_attempt_raw_wall_union_s": round(_union_length(closed_raw_intervals), 6),
        # A malformed shard line may itself be a stage marker.  Until a parser
        # can prove otherwise, treating the closed-attempt interval union as
        # complete would understate elapsed time with false confidence.
        "closed_attempt_union_partial": (
            not stage_coverage["coverage_complete"] or dropped_span_lines > 0
        ),
    }

    worker_config: dict[str, int] = {}
    observed: dict[str, int] = {}
    cache_hits = cache_misses = cache_hit_spans = 0
    provider_calls = provider_errors = total_attempts = 0
    by_reason: dict[str, int] = {}
    acquire_count = total_poll = 0
    total_wait = max_wait = total_held = 0.0
    dropped_attrs = 0

    for span in work_spans:
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
        "critical_path": compute_critical_path(work_spans, self_times),
        "by_category": _rollup(work_spans, self_times, "category"),
        "by_op": _rollup(work_spans, self_times, "op"),
        "total_self_time_s": round(sum(self_times.values()), 6),
        "stage_attempts": stage_attempts,
        "by_stage": by_stage,
        "stage_coverage": stage_coverage,
        "closed_attempt_metrics": closed_attempt_metrics,
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
