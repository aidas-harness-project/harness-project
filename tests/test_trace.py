"""Tests for tools/trace.py -- the span-recording core.

Each test here targets a property the plan named as a real risk, not merely
line coverage. The contextvars/ThreadPoolExecutor test in particular exists
because `concurrent.futures` silently drops context: without the copy_context
wrapper every page span records parent_span_id None, the aggregator sees
orphans instead of a tree, and the parallel page loop -- the most expensive
part of the pipeline -- becomes the part the instrumentation cannot explain.
"""
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import trace as trace_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_trace(monkeypatch):
    """Every test starts with tracing unconfigured and HARNESS_TRACE unset."""
    monkeypatch.delenv(trace_mod.TRACE_ENV, raising=False)
    trace_mod.reset()
    yield
    trace_mod.reset()


def _read_spans(root: Path, case_id="CASE_999", run_id="RUN_20260810_001"):
    spans_dir = root / case_id / "_trace" / run_id / "spans"
    out = []
    for shard in sorted(spans_dir.glob("*.jsonl")):
        for line in shard.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def _configure(tmp_path, case_id="CASE_999", run_id="RUN_20260810_001"):
    trace_mod.configure(case_id, run_id, root=tmp_path)
    return tmp_path


# ------------------------------------------------------------ basic writing --

def test_span_writes_one_record_with_duration(tmp_path):
    _configure(tmp_path)
    with trace_mod.span("ocr.page", category="io", doc_id="DOC_001", page=3):
        pass
    spans = _read_spans(tmp_path)
    assert len(spans) == 1
    rec = spans[0]
    assert rec["op"] == "ocr.page"
    assert rec["category"] == "io"
    assert rec["doc_id"] == "DOC_001"
    assert rec["page"] == 3
    assert rec["case_id"] == "CASE_999"
    assert rec["run_id"] == "RUN_20260810_001"
    assert rec["status"] == "ok"
    assert rec["duration_s"] >= 0
    # Both clocks are recorded: perf_counter for in-process duration, a wall
    # anchor because perf_counter is not comparable across processes.
    assert isinstance(rec["t_start_mono"], float)
    assert rec["t_start_wall"].endswith("+00:00")


def test_event_records_zero_duration(tmp_path):
    _configure(tmp_path)
    span_id = trace_mod.event("sla.phase1.start", category="marker",
                              marker_kind="sla_start")
    spans = _read_spans(tmp_path)
    assert len(spans) == 1
    assert spans[0]["duration_s"] == 0.0
    assert spans[0]["span_id"] == span_id
    assert spans[0]["attrs"] == {"marker_kind": "sla_start"}


def test_span_records_error_status_and_reraises(tmp_path):
    _configure(tmp_path)
    with pytest.raises(ValueError):
        with trace_mod.span("provider.transcribe_image", category="provider"):
            raise ValueError("boom")
    spans = _read_spans(tmp_path)
    # A failed call is exactly the kind of time that must show up in a rollup.
    assert spans[0]["status"] == "error"
    assert spans[0]["duration_s"] >= 0


def test_handle_set_attaches_attrs_measured_during_the_body(tmp_path):
    _configure(tmp_path)
    with trace_mod.span("provider.transcribe_image", category="provider",
                        provider_name="claude-cli") as sp:
        sp.set(output_chars=1234, attempts=2)
        sp.set_status("cache_hit")
    rec = _read_spans(tmp_path)[0]
    assert rec["attrs"]["provider_name"] == "claude-cli"
    assert rec["attrs"]["output_chars"] == 1234
    assert rec["attrs"]["attempts"] == 2
    assert rec["status"] == "cache_hit"


def test_error_status_wins_over_handle_status(tmp_path):
    _configure(tmp_path)
    with pytest.raises(RuntimeError):
        with trace_mod.span("provider.compare_text", category="provider") as sp:
            sp.set_status("cache_hit")
            raise RuntimeError("late failure")
    assert _read_spans(tmp_path)[0]["status"] == "error"


# ------------------------------------------------------------------ nesting --

def test_nested_spans_link_parent_to_child(tmp_path):
    _configure(tmp_path)
    with trace_mod.span("stage.document_processing", category="compute") as outer:
        with trace_mod.span("ocr.page", category="io"):
            pass
    spans = {s["op"]: s for s in _read_spans(tmp_path)}
    assert spans["ocr.page"]["parent_span_id"] == spans["stage.document_processing"]["span_id"]
    assert spans["stage.document_processing"]["parent_span_id"] is None
    assert outer.span_id == spans["stage.document_processing"]["span_id"]


def test_current_span_id_is_restored_after_exit(tmp_path):
    _configure(tmp_path)
    assert trace_mod.current_span_id() is None
    with trace_mod.span("a", category="compute"):
        first = trace_mod.current_span_id()
        assert first is not None
        with trace_mod.span("b", category="compute"):
            assert trace_mod.current_span_id() != first
        assert trace_mod.current_span_id() == first
    assert trace_mod.current_span_id() is None


def test_explicit_parent_overrides_context(tmp_path):
    _configure(tmp_path)
    root = trace_mod.event("root", category="marker")
    with trace_mod.span("outer", category="compute"):
        with trace_mod.span("pinned", category="compute", parent=root):
            pass
    spans = {s["op"]: s for s in _read_spans(tmp_path)}
    assert spans["pinned"]["parent_span_id"] == root


# ------------------------------------------- contextvars across thread pools --

def test_thread_pool_without_context_copy_loses_the_parent(tmp_path):
    """The defect this module guards against, demonstrated.

    Kept as a test so the copy_context requirement is not merely asserted in a
    docstring: raw pool.submit really does drop the parent, and the next test
    shows run_in_context really does fix it."""
    _configure(tmp_path)

    def worker(i):
        with trace_mod.span("ocr.page", category="io", page=i):
            pass

    with trace_mod.span("pool.ocr_pages", category="compute"):
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(worker, range(3)))

    pages = [s for s in _read_spans(tmp_path) if s["op"] == "ocr.page"]
    assert len(pages) == 3
    assert all(p["parent_span_id"] is None for p in pages)


def test_run_in_context_propagates_parent_into_pool_workers(tmp_path):
    _configure(tmp_path)

    def worker(i):
        with trace_mod.span("ocr.page", category="io", page=i):
            pass

    with trace_mod.span("pool.ocr_pages", category="compute") as pool_span:
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(trace_mod.run_in_context(worker), i)
                       for i in range(3)]
            for f in futures:
                f.result()

    spans = _read_spans(tmp_path)
    pages = [s for s in spans if s["op"] == "ocr.page"]
    assert len(pages) == 3
    assert {p["parent_span_id"] for p in pages} == {pool_span.span_id}
    assert sorted(p["page"] for p in pages) == [0, 1, 2]


def test_pool_workers_write_to_distinct_shards(tmp_path):
    """Shard uniqueness is the entire justification for writing without a
    lock: one writer per file means there is no second writer to exclude."""
    _configure(tmp_path)
    barrier = threading.Barrier(4)

    def worker(i):
        barrier.wait(timeout=10)
        with trace_mod.span("ocr.page", category="io", page=i):
            pass

    with ThreadPoolExecutor(max_workers=4) as pool:
        for f in [pool.submit(trace_mod.run_in_context(worker), i) for i in range(4)]:
            f.result()

    spans_dir = tmp_path / "CASE_999" / "_trace" / "RUN_20260810_001" / "spans"
    shards = list(spans_dir.glob("*.jsonl"))
    assert len(shards) == 4
    # Each shard holds exactly the spans of its own writer.
    for shard in shards:
        lines = [l for l in shard.read_text(encoding="utf-8").splitlines() if l.strip()]
        ids = {json.loads(l)["shard_id"] for l in lines}
        assert len(ids) == 1
        assert ids == {shard.stem}


def test_shard_id_encodes_pid_and_thread(tmp_path):
    _configure(tmp_path)
    with trace_mod.span("x", category="compute"):
        pass
    rec = _read_spans(tmp_path)[0]
    assert rec["shard_id"].startswith(f"{os.getpid()}_{threading.get_ident()}_")
    assert rec["pid"] == os.getpid()
    assert rec["thread"] == threading.get_ident()


# ------------------------------------------------------- attrs allow-listing --

def test_disallowed_attr_keys_are_dropped_and_counted(tmp_path):
    _configure(tmp_path)
    with trace_mod.span("provider.transcribe_image", category="provider",
                        provider_name="claude-cli",
                        prompt_text="환자의 성명은 홍길동이고 진단명은...",
                        page_body="사고 경위: 계단에서 낙상"):
        pass
    rec = _read_spans(tmp_path)[0]
    assert rec["attrs"] == {"provider_name": "claude-cli"}
    assert rec["dropped_attrs"] == 2
    # The structural guarantee: the content never reaches the file at all.
    raw = (tmp_path / "CASE_999" / "_trace" / "RUN_20260810_001" / "spans")
    blob = "".join(p.read_text(encoding="utf-8") for p in raw.glob("*.jsonl"))
    assert "홍길동" not in blob
    assert "낙상" not in blob


def test_allow_list_is_per_category(tmp_path):
    """`wait_s` is legitimate on a lock span and meaningless on a provider
    span; the filter is keyed by category precisely so one category's
    vocabulary cannot be used to smuggle a value onto another."""
    _configure(tmp_path)
    with trace_mod.span("lock.acquire", category="lock", wait_s=1.5, poll_count=3):
        pass
    with trace_mod.span("provider.classify", category="provider", wait_s=1.5):
        pass
    spans = {s["op"]: s for s in _read_spans(tmp_path)}
    assert spans["lock.acquire"]["attrs"] == {"wait_s": 1.5, "poll_count": 3}
    assert spans["provider.classify"]["attrs"] == {}
    assert spans["provider.classify"]["dropped_attrs"] == 1


def test_free_string_on_a_non_enum_allowed_key_is_dropped(tmp_path):
    """`output_chars` is on the provider allow-list but is a number. Passing
    prose there must not stringify into the record."""
    _configure(tmp_path)
    with trace_mod.span("provider.transcribe_image", category="provider",
                        output_chars="the full page said 홍길동"):
        pass
    rec = _read_spans(tmp_path)[0]
    assert rec["attrs"] == {}
    assert rec["dropped_attrs"] == 1


def test_enum_attr_is_sanitised_and_length_capped(tmp_path):
    """Sanitising must be ASCII-only, not `isalnum()`-based.

    `str.isalnum()` returns True for Hangul, so an isalnum filter would pass
    Korean case content through a key that is documented as a closed enum --
    the exact leak the allow-list exists to prevent. Every legitimate value on
    these keys (provider ids, model names, schema filenames) is ASCII."""
    _configure(tmp_path)
    with trace_mod.span("provider.transcribe_image", category="provider",
                        model_name="claude sonnet 5 (환자 홍길동 페이지)"):
        pass
    rec = _read_spans(tmp_path)[0]
    value = rec["attrs"]["model_name"]
    assert value.isascii()
    assert "홍길동" not in value
    assert " " not in value
    assert len(value) <= 64
    assert value.startswith("claudesonnet5")


def test_enum_attr_of_only_non_ascii_is_dropped_entirely(tmp_path):
    _configure(tmp_path)
    with trace_mod.span("provider.classify", category="provider",
                        model_name="환자기록"):
        pass
    rec = _read_spans(tmp_path)[0]
    assert rec["attrs"] == {}
    assert rec["dropped_attrs"] == 1


def test_long_enum_value_is_truncated(tmp_path):
    _configure(tmp_path)
    with trace_mod.span("validate.schema", category="validate",
                        schema_name="a" * 500):
        pass
    assert len(_read_spans(tmp_path)[0]["attrs"]["schema_name"]) == 64


def test_unknown_category_records_no_attrs(tmp_path):
    """Fail-closed: an unrecognised category has no allow-list, so it loses
    detail rather than leaking it."""
    _configure(tmp_path)
    with trace_mod.span("weird.op", category="not_a_category", anything=1):
        pass
    rec = _read_spans(tmp_path)[0]
    assert rec["attrs"] == {}
    assert rec["category"] == "compute"


def test_none_valued_attrs_are_omitted_not_dropped(tmp_path):
    _configure(tmp_path)
    with trace_mod.span("provider.classify", category="provider",
                        model_name=None, provider_name="codex-cli"):
        pass
    rec = _read_spans(tmp_path)[0]
    assert rec["attrs"] == {"provider_name": "codex-cli"}
    assert "dropped_attrs" not in rec


# ------------------------------------------------------------- disabled path --

def test_harness_trace_zero_is_a_full_no_op(tmp_path, monkeypatch):
    monkeypatch.setenv(trace_mod.TRACE_ENV, "0")
    trace_mod.configure("CASE_999", "RUN_20260810_001", root=tmp_path)
    assert trace_mod.enabled() is False
    with trace_mod.span("ocr.page", category="io") as sp:
        sp.set(output_chars=5)
        assert sp.span_id is None
    assert trace_mod.event("sla.phase1.start", category="marker") is None
    assert trace_mod.current_span_id() is None
    assert not (tmp_path / "CASE_999").exists()


@pytest.mark.parametrize("value", ["0", "false", "off", "no", "FALSE", "Off"])
def test_falsy_env_values_all_disable(tmp_path, monkeypatch, value):
    monkeypatch.setenv(trace_mod.TRACE_ENV, value)
    trace_mod.configure("CASE_999", "RUN_20260810_001", root=tmp_path)
    assert trace_mod.enabled() is False


def test_unset_env_means_enabled_once_configured(tmp_path, monkeypatch):
    monkeypatch.delenv(trace_mod.TRACE_ENV, raising=False)
    assert trace_mod.enabled() is False  # not configured yet
    trace_mod.configure("CASE_999", "RUN_20260810_001", root=tmp_path)
    assert trace_mod.enabled() is True


def test_spans_before_configure_are_dropped_silently(tmp_path):
    with trace_mod.span("ocr.page", category="io"):
        pass
    assert trace_mod.event("x", category="marker") is None
    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------- configure --

def test_configure_is_idempotent_for_the_same_run(tmp_path):
    _configure(tmp_path)
    with trace_mod.span("a", category="compute"):
        pass
    trace_mod.configure("CASE_999", "RUN_20260810_001", root=tmp_path)
    with trace_mod.span("b", category="compute"):
        pass
    spans = _read_spans(tmp_path)
    assert {s["op"] for s in spans} == {"a", "b"}
    # Same process + thread, so the same shard was reused rather than a second
    # one opened for the same writer.
    assert len({s["shard_id"] for s in spans}) == 1


def test_configure_switches_target_for_a_different_run(tmp_path):
    _configure(tmp_path, run_id="RUN_20260810_001")
    with trace_mod.span("a", category="compute"):
        pass
    trace_mod.configure("CASE_999", "RUN_20260810_002", root=tmp_path)
    with trace_mod.span("b", category="compute"):
        pass
    first = _read_spans(tmp_path, run_id="RUN_20260810_001")
    second = _read_spans(tmp_path, run_id="RUN_20260810_002")
    assert [s["op"] for s in first] == ["a"]
    assert [s["op"] for s in second] == ["b"]
    assert second[0]["run_id"] == "RUN_20260810_002"


def test_configure_never_raises_on_an_unusable_root(tmp_path):
    """Tracing is diagnostic. An unwritable trace directory disables it; it
    does not fail the run that was otherwise fine."""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    trace_mod.configure("CASE_999", "RUN_20260810_001", root=blocker)
    assert trace_mod.enabled() is False
    with trace_mod.span("a", category="compute"):
        pass


def test_emit_failure_does_not_propagate(tmp_path, monkeypatch):
    _configure(tmp_path)

    def explode(_shard):
        raise OSError("disk full")

    monkeypatch.setattr(trace_mod, "_handle_for", explode)
    with trace_mod.span("a", category="compute"):
        pass
    assert trace_mod.event("b", category="marker") is not None


# ------------------------------------------------------- concurrency probe --

def test_concurrency_probe_records_peak_not_total(tmp_path):
    probe = trace_mod.ConcurrencyProbe()
    barrier = threading.Barrier(3)

    def worker():
        with probe.enter():
            barrier.wait(timeout=10)

    with ThreadPoolExecutor(max_workers=3) as pool:
        for f in [pool.submit(worker) for _ in range(3)]:
            f.result()
    assert probe.max_observed == 3

    sequential = trace_mod.ConcurrencyProbe()
    for _ in range(5):
        with sequential.enter():
            pass
    assert sequential.max_observed == 1
