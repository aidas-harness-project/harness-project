"""Stage 2's expensive phases must stay traced.

Measured on CASE_911 (RUN_20260812_002), `document_processing` ran 897s and
the trace explained 192.6s of it -- 704.8s, 79%, was unattributable. The two
holes were structural, not accidental:

  * `segment_case.py` had exactly one span (`segment.judge`), so the `split`
    phase -- the single largest phase of the stage at 401s, 45% -- recorded
    only the 61.8s of LLM tier calls inside it.
  * `chunk_text.py` had no trace import at all and never called
    `configure_from_args`, so it recorded nothing and still exited 0, which
    is indistinguishable from a phase that had nothing to report. This is
    the exact failure `trace.configure_from_args`'s own docstring warns
    about ("exactly how redact_document.py lost the entire B1 measurement").

An unmeasured phase is not merely undocumented -- it is the phase most likely
to be optimized by guesswork, and the OCR width work in this same session
already showed what guessing costs (a short-call curve applied to a long-call
workload, 27% slower than the measured optimum).

These tests pin the instrumentation itself. They deliberately assert on the
DECORATION rather than on timing numbers, which would be flaky.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import chunk_text  # noqa: E402
import segment_case  # noqa: E402
import trace as trace_mod  # noqa: E402


# The phases that were dark, each with the reason it must stay lit.
SEGMENT_TRACED = {
    "split_bundle": "the 401s split phase -- largest single phase of Stage 2",
    "text_anchor_boundaries": "deterministic boundary derivation, 0 model calls",
    "_processed_page_texts": "reads every page of the bundle off disk",
    "_redistribute_parent_ocr": "hands the bundle's pages to each child",
    "_redistribute_parent_redaction": "same, for redacted text",
}
CHUNK_TRACED = {
    "chunk_document": "per-document chunking",
    "assemble_chunks": "the whole chunking pass",
}


@pytest.mark.parametrize("name,why", sorted(SEGMENT_TRACED.items()))
def test_segment_phase_is_traced(name: str, why: str) -> None:
    fn = getattr(segment_case, name)
    assert hasattr(fn, "__wrapped__"), (
        f"segment_case.{name} lost its @trace_mod.traced decorator -- {why}. "
        "Untraced, its cost lands in unattributed_active_s where it reads as "
        "model reasoning rather than as tool work."
    )


@pytest.mark.parametrize("name,why", sorted(CHUNK_TRACED.items()))
def test_chunk_phase_is_traced(name: str, why: str) -> None:
    fn = getattr(chunk_text, name)
    assert hasattr(fn, "__wrapped__"), (
        f"chunk_text.{name} lost its @trace_mod.traced decorator -- {why}."
    )


def test_chunk_text_configures_tracing_from_its_cli() -> None:
    """Decorating the functions is not enough on its own.

    A tool that never calls configure_from_args discards every span it
    produces -- including the dao/provider spans it does not own -- and exits
    0 regardless. chunk_text.py was in exactly that state.
    """
    src = (TOOLS / "chunk_text.py").read_text(encoding="utf-8")
    assert "configure_from_args" in src, (
        "chunk_text.py must configure tracing or its spans are silently dropped"
    )
    assert "--run-id" in src, (
        "configure_from_args needs a run_id; without one it returns False and "
        "records nothing rather than inventing an unreachable id"
    )


def test_chunking_emits_spans_end_to_end() -> None:
    """The property that actually matters: a real invocation writes shards.

    chunk_text.py resolves data/ and outputs/ from its own module location,
    not from cwd, so this cannot run inside tmp_path -- it uses a throwaway
    case id in the real tree and removes both directories afterwards. Nothing
    it touches belongs to a real case.

    Asserts the spans land under the run id that was PASSED, since a shard
    written under a different id is unreachable by `aggregate-trace --run-id`
    and is worse than not recording at all.
    """
    import shutil

    case_id, doc_id, run_id = "CASE_TRACETEST", "DOC_001", "RUN_TRACE_TEST_001"
    proc = ROOT / "data" / "processed" / case_id
    out = ROOT / "outputs" / case_id
    assert not proc.exists() and not out.exists(), (
        f"{case_id} already exists -- refusing to overwrite"
    )
    try:
        (proc / doc_id).mkdir(parents=True)
        (proc / doc_id / "redacted_text.md").write_text(
            "<<<PAGE page=1>>>\nfirst page text\n"
            "<<<PAGE page=2>>>\nsecond page text\n",
            encoding="utf-8",
        )
        out.mkdir(parents=True)

        env = {
            **dict(__import__("os").environ),
            "HARNESS_TRACE": "1",
            "PYTHONIOENCODING": "utf-8",
        }
        result = subprocess.run(
            [sys.executable, str(TOOLS / "chunk_text.py"), case_id, doc_id,
             "--run-id", run_id],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", env=env,
        )
        assert result.returncode == 0, result.stderr

        shard_dir = out / "_trace" / run_id / "spans"
        shards = list(shard_dir.glob("*.jsonl")) if shard_dir.exists() else []
        _assert_chunk_spans(shards, run_id)
    finally:
        shutil.rmtree(proc, ignore_errors=True)
        shutil.rmtree(out, ignore_errors=True)


def _assert_chunk_spans(shards: list[Path], run_id: str) -> None:
    assert shards, (
        f"no trace shards under {run_id} -- chunking recorded nothing while "
        "still exiting 0, the exact silent-discard failure this guards"
    )
    ops = {
        json.loads(line)["op"]
        for shard in shards
        for line in shard.read_text(encoding="utf-8").splitlines() if line.strip()
    }
    assert "chunk.assemble" in ops and "chunk.document" in ops, (
        f"expected chunk.* spans, got {sorted(ops)}"
    )


def test_traced_ops_use_declared_categories() -> None:
    """A span whose category is not in trace.CATEGORIES is dropped at
    aggregation, so a typo'd category is indistinguishable from no span."""
    src = (TOOLS / "segment_case.py").read_text(encoding="utf-8")
    src += (TOOLS / "chunk_text.py").read_text(encoding="utf-8")
    import re
    for category in re.findall(r'@trace_mod\.traced\("[^"]+",\s*category="(\w+)"', src):
        assert category in trace_mod.CATEGORIES, (
            f"category {category!r} is not one of {sorted(trace_mod.CATEGORIES)}"
        )
