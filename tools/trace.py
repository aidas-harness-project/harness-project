"""Performance instrumentation: span records written to per-writer shards.

This module answers one question the repo could not answer before it existed:
where does a Phase 1 run actually spend its wall-clock. Nothing here changes
pipeline behaviour -- a span is a timing observation, never a gate, and a
failure to record one must never fail a run (see `_emit`).

WHY THESE SHARDS ARE WRITTEN WITHOUT A LOCK (this is not a P5 bypass)
---------------------------------------------------------------------
P5 requires an exclusive lock before editing a *shared* file: a file more than
one writer can open for writing, where two writers racing would corrupt or
lose each other's data. That is what a lock exists to prevent.

A span shard is not such a file. `shard_id` is `{pid}_{thread_ident}_{hex}`
where the hex component comes from this process's `perf_counter_ns` at
configure time, so the path names exactly one thread of one process. No other
writer can ever open it, which makes the lock's purpose vacuous here: there is
no second writer to exclude. Acquiring a lock anyway would be actively harmful
for this specific file -- P5's cadence polls at 30s (dao.LOCK_POLL_INTERVAL_
SECONDS), so instrumenting a parallel page loop through a shared locked file
would serialise the very workers being measured and the measurement would
report the instrument's cost as the pipeline's.

This is the same argument that already makes `ocr_extract`'s per-page resume
cache safe and lock-free: one file per unit of work, one writer per file,
append/replace only, never read-modify-write by a second party.

Three further constraints keep this from drifting into a general-purpose
side-channel for pipeline state:

1. Shards are DIAGNOSTIC, not contract data. Nothing downstream reads them to
   make a decision. The only consumer is `dao.py aggregate-trace`, which runs
   once after the run and writes the real, permanent, schema-validated
   artifact (`_timing_summary.json`) through the ordinary locked DAO path. So
   every governed write still goes through the DAO; this module only stages
   raw observations for it.
2. A truncated or unparseable line is DISCARDED by the aggregator, not
   repaired. Losing a span must be cheaper than any attempt to protect one.
3. `attrs` are filtered against a per-category allow-list of keys whose values
   are restricted to numbers, booleans, closed enums and hashes -- see
   `_ALLOWED_ATTRS`. Free-form strings are structurally impossible, so a
   prompt body or a page of case material cannot reach a trace file even by
   mistake.

`trace.py` deliberately does NOT import `dao.py`. The DAO is one of the things
being measured, so the dependency runs dao -> trace, never the reverse.
"""
from __future__ import annotations

import contextlib
import contextvars
import functools
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = "0.1"

TRACE_ENV = "HARNESS_TRACE"

# Closed vocabulary. A span whose category is not one of these records no
# attrs at all (there is no allow-list to check it against), which is the
# fail-closed direction: an unknown category loses detail, never leaks it.
CATEGORIES = frozenset({
    "provider",
    "lock",
    "validate",
    "io",
    "subprocess",
    "compute",
    "human_wait",
    "marker",
    # An orchestrator's subagent dispatch: the interval from asking for the
    # work to holding its result. Its own category because it is neither the
    # agent's work (which the harness reports separately) nor a tool span, and
    # because the residual it explains is otherwise indistinguishable from
    # operator-side work done while a stage marker happened to be open.
    "dispatch",
})

# Per-category allowed attr keys (plan section 3.3). Anything not listed here
# is dropped, and the drop is COUNTED (`dropped_attrs`) so a rollup can show
# that detail went missing without revealing what it was.
_ALLOWED_ATTRS: dict[str, frozenset[str]] = {
    "provider": frozenset({
        "provider_name", "model_name", "prompt_version", "input_chars",
        "input_images", "output_chars", "timeout_s", "attempts",
        "retry_reason_code", "structured", "queue_wait_s", "unit_hash",
    }),
    "lock": frozenset({
        "lock_kind", "wait_s", "poll_count", "acquired", "held_s",
    }),
    "validate": frozenset({
        "schema_name", "registry_cache_hit", "error_count",
    }),
    "subprocess": frozenset({
        "argv0", "subcommand", "exit_code",
    }),
    "io": frozenset({
        "bytes_in", "bytes_out", "page_count", "exit_code", "startup_s",
        "hit_count", "documents_searched", "unit_hash",
    }),
    "compute": frozenset({
        "worker_count", "observed_max_concurrency", "items",
        "cache_hits", "cache_misses", "unit_hash",
    }),
    "human_wait": frozenset({
        "gate_kind", "waited_s",
    }),
    # agent_reported_s is what the harness says the subagent itself took; the
    # span's own duration is the whole dispatch. The difference is round-trip
    # cost, which is the only thing this category exists to expose.
    #
    # The token counts are the same kind of value: HARNESS-REPORTED, not
    # measured here. They exist because an agent stage's wall time tracks token
    # volume and not tool latency -- claim_analysis spent 773.0s of which 1.20s
    # (0.15%) was tools. Without them the only available reading of the
    # remainder is `unattributed_active_s`, which T13 says explicitly is not a
    # claim about model reasoning; a count of what was read and written is the
    # first figure that can be one. Counts only: no prompt, no completion, no
    # case content -- an int cannot carry prose past _clean_attrs.
    "dispatch": frozenset({
        "stage_name", "agent_kind", "agent_reported_s", "attempt",
        "input_tokens", "output_tokens", "total_tokens", "tool_uses",
        # Time the dispatch sat blocked on a human (a permission prompt, a
        # gate answer). Subtracted before any rate, because a prompt the
        # operator took 8 minutes to answer is not model work: on CASE_027 it
        # was 506s of an 862s stage and made 411 tok/s report as 170.
        "human_wait_s",
    }),
    "marker": frozenset({
        "marker_kind", "stage_name", "attempt_outcome",
    }),
}

# Attr keys whose value is a bounded identifier rather than a number. They are
# accepted as strings, but length-capped and stripped down to ASCII identifier
# characters, so a caller cannot smuggle prose (or a filesystem path, or a
# prompt) through a key that happens to be on the allow-list.
#
# ASCII specifically, not `str.isalnum()`: `isalnum()` is True for Hangul and
# CJK, so an isalnum-based filter passes Korean case content through
# unchanged -- which is the one thing this filter exists to stop. Every value
# these keys legitimately carry (provider names, model ids, schema filenames,
# dao subcommands, prompt versions) is ASCII by construction, so the cost of
# the narrower rule is zero and the guarantee becomes checkable by eye.
_ENUM_ATTRS = frozenset({
    "provider_name", "model_name", "prompt_version", "retry_reason_code",
    "lock_kind", "schema_name", "argv0", "subcommand", "gate_kind",
    "marker_kind", "stage_name", "attempt_outcome",
    "unit_hash", "agent_kind",
})
_ENUM_MAX_LEN = 64
_ENUM_EXTRA_CHARS = frozenset("._-:/")


def _sanitise_enum(value: str) -> str:
    return "".join(
        ch for ch in value.strip()
        if (ch.isascii() and ch.isalnum()) or ch in _ENUM_EXTRA_CHARS
    )[:_ENUM_MAX_LEN]

STATUSES = frozenset({"ok", "error", "cache_hit", "skipped"})

_current_span_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "harness_trace_current_span_id", default=None
)

_state_lock = threading.Lock()
_state: dict[str, Any] = {
    "configured": False,
    "case_id": None,
    "run_id": None,
    "spans_dir": None,
}

# Stage ownership is resolved from the marker directory the DAO already
# writes, NOT from an environment variable. A stage spans processes the
# orchestrator does not fork -- a subagent's tool call is a fresh session, and
# each shell invocation starts a fresh environment -- so an exported variable
# provably never reaches the tools whose spans need attributing. The marker
# files are the one channel every process can see.
#
# Resolution is cached per (mtime, size) of the marker directory rather than
# read once at configure(): a long-lived process (run_stage2) outlives several
# stage transitions, and a value frozen at startup would mislabel every span
# after the first one.
_stage_cache_lock = threading.Lock()
_stage_cache: dict[str, Any] = {"signature": None, "stages": ()}
_STAGE_CACHE_TTL_S = 2.0
# One open file handle per (pid, thread) shard. Keyed by shard id so a thread
# that outlives many spans pays the open cost once.
_handles: dict[str, Any] = {}
_handles_lock = threading.Lock()


def enabled() -> bool:
    """False when HARNESS_TRACE is set to a falsy value, or when configure()
    has not run. Unset means ON: the SLA measurement work wants instrumentation
    by default, and HARNESS_TRACE=0 is the documented off switch (and the
    documented rollback for the whole of T0)."""
    raw = str(os.environ.get(TRACE_ENV, "")).strip().lower()
    if raw in {"0", "false", "off", "no"}:
        return False
    return bool(_state["configured"])


def configure(case_id: str, run_id: str, root: Path | str | None = None) -> None:
    """Point the shard writer at outputs/<case_id>/_trace/<run_id>/spans/.

    Idempotent for the same (case_id, run_id): calling it again is a no-op, so
    a tool that configures at entry and a library it calls that configures
    defensively do not fight. Re-configuring for a DIFFERENT case or run
    switches target and drops the cached handles, since the old shard belongs
    to the old run.

    Never raises. Tracing is diagnostic; if the directory cannot be created,
    tracing stays off and the run proceeds.
    """
    if str(os.environ.get(TRACE_ENV, "")).strip().lower() in {"0", "false", "off", "no"}:
        return
    base = Path(root) if root is not None else Path(__file__).resolve().parent.parent / "outputs"
    spans_dir = base / case_id / "_trace" / run_id / "spans"
    with _state_lock:
        if (_state["configured"] and _state["case_id"] == case_id
                and _state["run_id"] == run_id):
            return
        try:
            spans_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        _close_handles()
        _state.update({
            "configured": True,
            "case_id": case_id,
            "run_id": run_id,
            "spans_dir": spans_dir,
        })
    # Switching case/run points at a different marker directory; a cached
    # resolution from the previous run would label the new run's spans.
    with _stage_cache_lock:
        _stage_cache.update({"signature": None, "stages": ()})


def reset() -> None:
    """Drop all configuration and close open shards. For tests and for a tool
    that processes several cases in one process."""
    with _state_lock:
        _close_handles()
        _state.update({"configured": False, "case_id": None, "run_id": None,
                       "spans_dir": None})
    with _stage_cache_lock:
        _stage_cache.update({"signature": None, "stages": ()})


def _close_handles() -> None:
    with _handles_lock:
        for handle in _handles.values():
            with contextlib.suppress(Exception):
                handle.close()
        _handles.clear()


def current_span_id() -> str | None:
    """The innermost open span in THIS context. Propagates into a thread only
    when the caller copies the context -- see run_in_context()."""
    return _current_span_id.get()


def _read_open_stages() -> tuple[tuple[str, int], ...]:
    """Stage attempts with a start marker and no terminal marker, from disk.

    Returns ((stage_name, attempt), ...) sorted, or () when nothing can be
    determined. Never raises: an unreadable marker directory means spans go out
    unattributed, which is the pre-existing behaviour, not a failure.

    Filenames are the source of truth here rather than file contents. The DAO
    writes them as `stage.attempt.<kind>.<stage>.<attempt>.json` under O_EXCL,
    so the name alone answers the question and a torn or half-written body
    cannot make a stage look open when it is not.
    """
    spans_dir = _state["spans_dir"]
    if spans_dir is None:
        return ()
    marker_dir = Path(spans_dir).parent / "markers"
    try:
        names = [p.name for p in marker_dir.iterdir() if p.is_file()]
    except OSError:
        return ()

    started: dict[tuple[str, int], None] = {}
    terminal: set[tuple[str, int]] = set()
    for name in names:
        if not name.startswith("stage.attempt.") or not name.endswith(".json"):
            continue
        body = name[len("stage.attempt."):-len(".json")]
        kind, _, rest = body.partition(".")
        if kind not in ("start", "end", "abandoned") or not rest:
            continue
        stage, _, attempt_text = rest.rpartition(".")
        if not stage or not attempt_text.isdigit():
            continue
        key = (stage, int(attempt_text))
        if kind == "start":
            started[key] = None
        else:
            terminal.add(key)
    return tuple(sorted(k for k in started if k not in terminal))


def _open_stages() -> tuple[tuple[str, int], ...]:
    """_read_open_stages() behind a short cache keyed on directory state.

    Every span would otherwise stat the marker directory. The signature is
    (mtime_ns, entry count) plus a short TTL: a marker landing inside the TTL
    without changing either is only possible if one appeared and another was
    removed in the same window, and markers are never removed.
    """
    spans_dir = _state["spans_dir"]
    if spans_dir is None:
        return ()
    marker_dir = Path(spans_dir).parent / "markers"
    try:
        stat = marker_dir.stat()
        signature = (str(marker_dir), stat.st_mtime_ns,
                     int(time.monotonic() / _STAGE_CACHE_TTL_S))
    except OSError:
        signature = (str(marker_dir), None,
                     int(time.monotonic() / _STAGE_CACHE_TTL_S))
    with _stage_cache_lock:
        if _stage_cache["signature"] == signature:
            return _stage_cache["stages"]
    stages = _read_open_stages()
    with _stage_cache_lock:
        _stage_cache.update({"signature": signature, "stages": stages})
    return stages


def _shard_id() -> str:
    return f"{os.getpid()}_{threading.get_ident()}_{time.perf_counter_ns():x}"


_thread_shard: threading.local = threading.local()


def _shard_for_thread() -> str:
    shard = getattr(_thread_shard, "shard_id", None)
    if shard is None:
        shard = _shard_id()
        _thread_shard.shard_id = shard
    return shard


def _handle_for(shard: str):
    with _handles_lock:
        handle = _handles.get(shard)
        if handle is not None:
            return handle
        spans_dir = _state["spans_dir"]
        if spans_dir is None:
            return None
        try:
            handle = open(spans_dir / f"{shard}.jsonl", "a", encoding="utf-8")
        except OSError:
            return None
        _handles[shard] = handle
        return handle


def _clean_attrs(category: str, attrs: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Filter attrs to the category's allow-list and coerce values into the
    permitted shapes. Returns (kept, dropped_count).

    This is the structural half of "no prompt text and no case content in a
    trace file". A key not on the list is dropped without recording what it
    was; a listed key whose value is not int/float/bool and not a short
    identifier is also dropped rather than stringified, because stringifying
    an arbitrary object is exactly how prose would get in."""
    allowed = _ALLOWED_ATTRS.get(category, frozenset())
    kept: dict[str, Any] = {}
    dropped = 0
    for key, value in attrs.items():
        if value is None:
            continue
        if key not in allowed:
            dropped += 1
            continue
        if isinstance(value, bool):
            kept[key] = value
        elif isinstance(value, int):
            kept[key] = value
        elif isinstance(value, float):
            kept[key] = round(value, 6)
        elif key in _ENUM_ATTRS and isinstance(value, str):
            token = _sanitise_enum(value)
            if token:
                kept[key] = token
            else:
                dropped += 1
        else:
            dropped += 1
    return kept, dropped


def _emit(record: dict[str, Any]) -> None:
    """Write one span line. Swallows every error: a lost span is a lost
    measurement, and a lost measurement must never be allowed to fail a
    pipeline run that was otherwise fine."""
    try:
        handle = _handle_for(record["shard_id"])
        if handle is None:
            return
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
    except Exception:
        return


def _build_record(op: str, category: str, *, case_id, doc_id, page, parent,
                  duration_s: float, status: str, attempt, t_start_wall: str,
                  t_start_mono: float, attrs: dict[str, Any]) -> dict[str, Any]:
    cleaned, dropped = _clean_attrs(category, attrs)
    record = {
        "schema_version": SCHEMA_VERSION,
        "span_id": uuid.uuid4().hex,
        "parent_span_id": parent,
        "run_id": _state["run_id"],
        "case_id": case_id if case_id is not None else _state["case_id"],
        "doc_id": doc_id,
        "page": page,
        "op": op,
        "category": category if category in CATEGORIES else "compute",
        "t_start_wall": t_start_wall,
        "t_start_mono": round(t_start_mono, 9),
        "duration_s": round(duration_s, 9),
        "status": status if status in STATUSES else "ok",
        "attempt": attempt,
        "shard_id": _shard_for_thread(),
        "pid": os.getpid(),
        "thread": threading.get_ident(),
        "attrs": cleaned,
    }
    if dropped:
        record["dropped_attrs"] = dropped
    # Explicit stage ownership, so the aggregator does not have to infer it
    # from overlapping wall-clock windows (which it cannot do, and correctly
    # refuses to guess at). A stage marker is excluded: it already names its
    # own stage, and reading the marker set to label a marker is circular.
    if category != "marker" and not op.startswith("stage.attempt."):
        open_stages = _open_stages()
        if len(open_stages) == 1:
            record["stage_name"] = open_stages[0][0]
            record["stage_attempt"] = open_stages[0][1]
        elif len(open_stages) > 1:
            # Concurrent stages: this span belongs to exactly one of them and
            # nothing on disk says which. Recording the candidate set keeps the
            # ambiguity visible and bounded instead of discarding the span's
            # attribution entirely or picking one and being silently wrong.
            record["stage_candidates"] = [
                {"stage_name": name, "attempt": attempt}
                for name, attempt in open_stages
            ]
    return record


class _SpanHandle:
    """Returned by span(). Lets the body attach measured values that are only
    known once the work has run (output size, retry count, cache outcome)."""

    __slots__ = ("span_id", "attrs", "status")

    def __init__(self, span_id: str | None, attrs: dict[str, Any]):
        self.span_id = span_id
        self.attrs = attrs
        self.status = "ok"

    def set(self, **attrs: Any) -> None:
        self.attrs.update(attrs)

    def set_status(self, status: str) -> None:
        self.status = status


_NOOP_HANDLE = _SpanHandle(None, {})


@contextlib.contextmanager
def span(op: str, *, category: str, case_id: str | None = None,
         doc_id: str | None = None, page: int | None = None,
         parent: str | None = None, attempt: int | None = None,
         **attrs: Any) -> Iterator[_SpanHandle]:
    """Time a block and record it as one span.

    When tracing is off this yields a shared no-op handle and does nothing
    else -- no timestamp calls, no allocation of a per-call handle, no
    contextvar set/reset. That is what keeps the disabled path near-free on a
    hot page loop.

    A span is recorded even when the body raises (status "error"), because a
    failed provider call is exactly the kind of time that has to appear in the
    rollup. The exception then propagates unchanged.
    """
    if not enabled():
        yield _NOOP_HANDLE
        return

    parent_id = parent if parent is not None else _current_span_id.get()
    handle = _SpanHandle(uuid.uuid4().hex, dict(attrs))
    t_start_wall = datetime.now(timezone.utc).isoformat()
    t_start_mono = time.perf_counter()
    token = _current_span_id.set(handle.span_id)
    status = "ok"
    try:
        yield handle
    except BaseException:
        status = "error"
        raise
    finally:
        duration = time.perf_counter() - t_start_mono
        _current_span_id.reset(token)
        final_status = status if status == "error" else handle.status
        record = _build_record(
            op, category, case_id=case_id, doc_id=doc_id, page=page,
            parent=parent_id, duration_s=duration, status=final_status,
            attempt=attempt, t_start_wall=t_start_wall,
            t_start_mono=t_start_mono, attrs=handle.attrs,
        )
        record["span_id"] = handle.span_id
        _emit(record)


def event(op: str, *, category: str, case_id: str | None = None,
          doc_id: str | None = None, page: int | None = None,
          status: str = "ok", attempt: int | None = None,
          **attrs: Any) -> str | None:
    """Record a zero-duration point in time (an SLA marker, a cache hit, a
    gate opening). Returns the span id, or None when tracing is off."""
    if not enabled():
        return None
    now_mono = time.perf_counter()
    record = _build_record(
        op, category, case_id=case_id, doc_id=doc_id, page=page,
        parent=_current_span_id.get(), duration_s=0.0, status=status,
        attempt=attempt, t_start_wall=datetime.now(timezone.utc).isoformat(),
        t_start_mono=now_mono, attrs=attrs,
    )
    _emit(record)
    return record["span_id"]


def closed_interval(op: str, *, category: str, t_start_wall: str,
                    duration_s: float, case_id: str | None = None,
                    status: str = "ok", **attrs: Any) -> str | None:
    """Record an interval that already happened, anchored to a past wall time.

    `span()` measures work this process is doing now, and `event()` is a point
    with no duration. Neither can express "a human gate was open from 09:00 to
    09:40" -- that interval is only knowable once it closes, and it must land
    where it occurred so an aggregator can subtract it from a window that
    contains it.

    The wall anchor is authoritative here, not perf_counter: the interval may
    span processes (a gate opened by one command and closed by another), and
    monotonic clocks are not comparable across them.
    """
    if not enabled():
        return None
    record = _build_record(
        op, category, case_id=case_id, doc_id=None, page=None,
        parent=_current_span_id.get(), duration_s=max(0.0, float(duration_s)),
        status=status, attempt=None, t_start_wall=t_start_wall,
        t_start_mono=time.perf_counter(), attrs=attrs,
    )
    _emit(record)
    return record["span_id"]


def run_in_context(fn):
    """Wrap `fn` so it runs under a COPY of the caller's current context.

    `concurrent.futures` does not propagate `contextvars` to worker threads.
    Without this, every span raised inside a pool worker records
    `parent_span_id: None` and the aggregator sees a forest of orphans instead
    of a tree -- the critical-path calculation then has nothing to walk, so
    the single most expensive part of the pipeline (the parallel page loop)
    becomes the part the instrumentation cannot explain.

    Usage: `pool.submit(trace.run_in_context(worker), arg)`.

    The parent span id is captured ONCE here, at wrap time, but each call gets
    a FRESH context to run in. A single `contextvars.Context` object cannot be
    entered more than once concurrently -- reusing one across pool workers
    raises "cannot enter context: ... is already entered" the moment two
    workers overlap, which is every run that actually parallelises. Copying
    per call is also the more accurate semantics: a worker's own nested spans
    belong to that worker, not to a context shared with its siblings.
    """
    parent_id = _current_span_id.get()

    def _wrapped(*args, **kwargs):
        ctx = contextvars.copy_context()

        def _inner():
            _current_span_id.set(parent_id)
            return fn(*args, **kwargs)

        return ctx.run(_inner)

    return _wrapped


def configure_from_args(args, *, case_id: str | None = None,
                        run_id: str | None = None) -> bool:
    """Switch tracing on from a CLI's parsed arguments. Returns whether it did.

    Every CLI entry point that can reach an instrumented code path must call
    this, and "instrumented" includes paths it does not own: merely calling
    `dao` emits lock.acquire and validate.schema spans, and calling a provider
    emits provider.* spans. A tool that never configures silently discards all
    of them and still exits 0, which is indistinguishable from a run that had
    nothing to report -- exactly how redact_document.py lost the entire B1
    measurement.

    Both ids are required and neither is invented. A missing run_id used to be
    tempting to synthesize, but shards written under a made-up id land where
    `aggregate-trace --run-id` will never look, which is worse than not
    recording: the run appears traced and the data is unreachable.
    """
    resolved_case = case_id or getattr(args, "case_id", None)
    resolved_run = run_id or getattr(args, "run_id", None)
    if not resolved_case or not resolved_run:
        return False
    configure(resolved_case, resolved_run)
    return enabled()


def traced(op: str, *, category: str = "compute"):
    """Decorator form of span(), for wrapping a whole function.

    Used where the function body is long enough that opening a `with` around
    it would mean reindenting hundreds of lines across several early returns
    -- a large mechanical edit to production code in exchange for a
    measurement, which is the wrong trade on a write path.
    """
    def decorate(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if not enabled():
                return fn(*args, **kwargs)
            with span(op, category=category):
                return fn(*args, **kwargs)
        return wrapper
    return decorate


class ConcurrencyProbe:
    """Counts observed simultaneous entries into a region.

    The point of recording this next to the configured worker count is that
    the two disagreeing IS the finding: a pool told to run 4 workers whose
    observed maximum is 1 is a pool serialised by something else (a lock, a
    semaphore, a provider queue), which is precisely what the plan's worker
    sweep needs to see."""

    __slots__ = ("_lock", "_active", "max_observed")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = 0
        self.max_observed = 0

    @contextlib.contextmanager
    def enter(self) -> Iterator[None]:
        with self._lock:
            self._active += 1
            if self._active > self.max_observed:
                self.max_observed = self._active
        try:
            yield
        finally:
            with self._lock:
                self._active -= 1
