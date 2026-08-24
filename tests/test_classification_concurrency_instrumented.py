"""Both classification pools must record the width they actually reached.

known-gaps #57. `_timing_summary.json` carried `worker_config` and
`observed_max_concurrency` for `ocr_pages`, `redact_pages`, `documents` and
`redaction` -- and nothing for classification, even though both classification
pools already emitted a span. The phase was visible; its width was not.

That mattered on 2026-08-24, when `DEFAULT_CLASSIFY_WORKERS` was raised 4 -> 8
on a bench measurement. Whether a real run ever reaches 8 could not be answered
from retained records -- which is the same gap that let the OLD value's
justification ("the semaphore is the real ceiling anyway") stand unchallenged
for as long as it did. A configured width is an intention; only the observed
maximum says whether something upstream serialised it.

These tests assert on the instrumentation, not on timing numbers, which would
be flaky. The overlap is forced with a barrier held open across the probed
region: releasing threads at the same time is not enough, because they can
still enter and leave one at a time and leave the observed maximum at 1 -- the
very reading the probe exists to distinguish from real serialisation.
"""
from __future__ import annotations

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import trace as trace_mod  # noqa: E402
import trace_aggregate  # noqa: E402


CASE_ID = "CASE_999"
RUN_ID = "RUN_20260824_001"

# The pools this gap was filed about, each with why its width must be visible.
CLASSIFICATION_POOLS = {
    "pool.classification": "run_document_stage, the width raised 4 -> 8",
    "pool.classify_children": "run_stage2, the split-child path",
}


@pytest.fixture(autouse=True)
def _clean_trace(monkeypatch):
    """Every test starts with tracing unconfigured and HARNESS_TRACE unset."""
    monkeypatch.delenv(trace_mod.TRACE_ENV, raising=False)
    trace_mod.reset()
    yield
    trace_mod.reset()


def _read_spans(root: Path):
    spans_dir = root / CASE_ID / "_trace" / RUN_ID / "spans"
    out = []
    for shard in sorted(spans_dir.glob("*.jsonl")):
        for line in shard.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def _drive_pool(op: str, width: int, root: Path) -> None:
    """Run `width` genuinely-overlapping workers under a probed pool span.

    Mirrors the shape both real pools use: probe created outside the worker,
    `enter()` around the per-item region, `set()` after the pool drains.
    """
    trace_mod.configure(CASE_ID, RUN_ID, root=root)
    concurrency = trace_mod.ConcurrencyProbe()
    inside = threading.Barrier(width, timeout=10)

    def worker(i):
        # Barrier INSIDE the probed region: all workers are provably inside
        # `enter()` at the same instant. Without this the observed maximum can
        # legitimately be 1 even at width 8.
        with concurrency.enter():
            inside.wait()

    with trace_mod.span(op, category="compute", case_id=CASE_ID,
                        worker_count=width, items=width) as pool_span:
        with ThreadPoolExecutor(max_workers=width) as pool:
            submit = trace_mod.run_in_context(worker)
            futures = [pool.submit(submit, i) for i in range(width)]
            for future in futures:
                future.result()
        pool_span.set(observed_max_concurrency=concurrency.max_observed)


def _summarize(root: Path) -> dict:
    return trace_aggregate.summarize(
        _read_spans(root), case_id=CASE_ID, run_id=RUN_ID,
        generated_at="2026-08-24T00:00:00Z",
        dropped_span_lines=0, shard_count=1)


@pytest.mark.parametrize("op", sorted(CLASSIFICATION_POOLS))
def test_the_pool_span_records_the_width_it_reached(op, tmp_path):
    """The attribute must be present AND equal the real overlap.

    Asserting presence alone would pass against a hardcoded 0 or a copy of
    worker_count; pinning it to the barrier width makes the probe prove it
    counted something.
    """
    _drive_pool(op, 4, tmp_path)

    pools = [s for s in _read_spans(tmp_path) if s["op"] == op]
    assert len(pools) == 1, f"{op}: expected exactly one pool span"
    attrs = pools[0].get("attrs") or {}
    assert attrs.get("observed_max_concurrency") == 4, (
        f"{op} ({CLASSIFICATION_POOLS[op]}) did not record the width it "
        f"reached; attrs={attrs}")
    assert attrs.get("worker_count") == 4


@pytest.mark.parametrize("op", sorted(CLASSIFICATION_POOLS))
def test_the_attribute_survives_the_trace_attr_allow_list(op, tmp_path):
    """`attrs` are filtered against a per-category allow-list before a span is
    written. An attribute the writer sets but the filter drops is worse than a
    missing one -- the code reads as instrumented and the record stays empty."""
    _drive_pool(op, 2, tmp_path)

    pools = [s for s in _read_spans(tmp_path) if s["op"] == op]
    assert "observed_max_concurrency" in (pools[0].get("attrs") or {})
    assert not pools[0].get("dropped_attrs")


@pytest.mark.parametrize("op", sorted(CLASSIFICATION_POOLS))
def test_the_aggregator_keys_it_under_the_pool_name(op, tmp_path):
    """The summary must carry it keyed by pool name.

    The aggregator derives both maps generically from any `pool.*` span, so
    this is what turns the span attribute into the field #57 asked for. The
    key is the pool name with the `pool.` prefix stripped.
    """
    _drive_pool(op, 3, tmp_path)
    name = op.split(".", 1)[1]

    summary = _summarize(tmp_path)

    assert summary["worker_config"].get(name) == 3
    assert summary["observed_max_concurrency"].get(name) == 3


@pytest.mark.parametrize("op", sorted(CLASSIFICATION_POOLS))
def test_a_serialised_pool_reads_below_its_configured_width(op, tmp_path):
    """The finding this field exists to make must be reachable.

    A pool configured wide whose observed maximum stays at 1 is serialised by
    something else. If the two numbers could never diverge the field would be
    decoration, so drive the probe with no overlap and confirm it reports 1
    against a configured 4 rather than echoing the configuration.
    """
    trace_mod.configure(CASE_ID, RUN_ID, root=tmp_path)
    concurrency = trace_mod.ConcurrencyProbe()
    gate = threading.Lock()

    def worker(i):
        # A lock held across the region: the pool has 4 workers, but only one
        # is ever inside at a time -- exactly the shape of a provider
        # semaphore or a manifest lock upstream of the pool.
        with gate, concurrency.enter():
            pass

    with trace_mod.span(op, category="compute", case_id=CASE_ID,
                        worker_count=4, items=4) as pool_span:
        with ThreadPoolExecutor(max_workers=4) as pool:
            submit = trace_mod.run_in_context(worker)
            for future in [pool.submit(submit, i) for i in range(4)]:
                future.result()
        pool_span.set(observed_max_concurrency=concurrency.max_observed)

    name = op.split(".", 1)[1]
    summary = _summarize(tmp_path)

    assert summary["worker_config"][name] == 4
    assert summary["observed_max_concurrency"][name] == 1, (
        "a fully serialised pool must not report its configured width")


# --------------------------------------------------- the REAL call sites --
#
# Everything above drives a synthetic pool, which proves the probe and the
# aggregator work but says nothing about whether the two real pools use them:
# with `run_document_stage.py` and `run_stage2.py` reverted to their
# pre-fix state, all of the tests above still passed. These bind to the
# actual source, the way test_stage2_instrumentation.py pins its decorators.

REAL_POOLS = {
    "run_document_stage.py": (
        "pool.classification",
        "the pool whose width was raised 4 -> 8 on a bench measurement",
    ),
    "run_stage2.py": (
        "pool.classify_children",
        "the split-child classification path",
    ),
}


@pytest.mark.parametrize("filename,spec", sorted(REAL_POOLS.items()))
def test_the_real_pool_probes_its_own_concurrency(filename, spec):
    """The tool must construct a probe and report it on its pool span.

    Three things have to be true together, and each has failed somewhere in
    this repo before: the probe exists, the pool span is bound to a name (a
    bare `with span(...)` cannot carry a later `set()`), and the observed
    maximum is actually written back.
    """
    op, why = spec
    src = (TOOLS / filename).read_text(encoding="utf-8")

    assert "ConcurrencyProbe()" in src, (
        f"{filename} never constructs a ConcurrencyProbe -- {op} ({why}) "
        "records a configured width with nothing to compare it against")
    assert "concurrency.enter()" in src, (
        f"{filename} constructs a probe but never enters it, so its maximum "
        "stays 0 no matter how wide the pool runs")
    assert "observed_max_concurrency=concurrency.max_observed" in src, (
        f"{filename} measures the width but never reports it on the span")


@pytest.mark.parametrize("filename,spec", sorted(REAL_POOLS.items()))
def test_the_real_pool_span_is_bound_and_carries_item_count(filename, spec):
    """`items` alongside `worker_count` is what makes the width readable.

    A pool span is capped by `min(workers, items)` in every one of these
    tools, so an observed maximum of 3 against a configured 8 is only a
    finding if the workload had more than 3 items. Without `items` the two
    readings -- "serialised" and "only had 3 documents" -- are the same
    record. run_document_stage's span omitted it.
    """
    op, _why = spec
    src = (TOOLS / filename).read_text(encoding="utf-8")

    start = src.index(f'trace_mod.span("{op}"')
    header = src[start:src.index(":\n", start)]
    assert "items=" in header, (
        f'{filename}: {op} does not record `items`, so a low observed '
        "maximum cannot be told apart from a small workload")
    assert "as pool_span" in header, (
        f"{filename}: {op} is not bound to a name, so nothing can attach the "
        "observed maximum to it after the pool drains")
