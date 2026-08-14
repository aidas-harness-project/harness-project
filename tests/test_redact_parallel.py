"""T4c: page-level parallelism in checkpoint 2.

Modelled on tests/test_ocr_parallel.py, with one addition that has no OCR
equivalent: redaction HARD-FAILS on a detected leak, so the parallel path has
to preserve "a leaking document writes nothing" even when other workers have
already finished successfully. Draining before re-raising must not be allowed
to turn a blocked document into a successful one.
"""
import json
import threading
import time

import pytest

import redact_document as rd
from redaction import RedactionLeakError, RedactionOutcome


def _pages(n):
    return {"cross_validation_status": "agreed",
            "pages": [{"page": i + 1} for i in range(n)]}


def _stub_multi_page(monkeypatch, tmp_path, n, page_text_for):
    """DAO stubs for an n-page document whose text differs per page."""
    captured = {}

    def fake_read_contract(case_id, filename):
        return _pages(n) if filename.startswith("ocr_result") else {}

    def fake_read_page_text(case_id, doc_id, page, *, caller_stage, capability):
        assert capability, "capability must survive the parallel path"
        assert caller_stage == "document-pipeline"
        return page_text_for(page)

    def fake_dao(*args, capability=None):
        assert capability is None
        if args[0] == "write-redacted-text":
            captured["redacted"] = rd.Path(
                args[args.index("--text-file") + 1]).read_text(encoding="utf-8")
        if args[0] == "write-contract":
            captured["contract"] = json.loads(rd.Path(
                args[args.index("--data-file") + 1]).read_text(encoding="utf-8"))
        return "OK"

    monkeypatch.setattr(rd.dao, "read_contract_data", fake_read_contract)
    monkeypatch.setattr(rd.dao, "read_page_text_data", fake_read_page_text)
    monkeypatch.setattr(rd, "_dao", fake_dao)
    monkeypatch.setattr(rd, "ROOT", tmp_path)
    return captured


def _page_of(text):
    return int(text.split("PAGE")[1].split()[0])


class _OrderScramblingRedactor:
    """Finishes later pages first, so any reliance on completion order shows up
    as mis-filed page text rather than passing by luck."""

    method = "llm_span_redaction"
    label = "llm_span_redaction:fixture:scrambler"

    def __init__(self, total):
        self.total = total
        self.seen = []
        self._lock = threading.Lock()

    def redact_page(self, text):
        page = _page_of(text)
        time.sleep((self.total - page) * 0.01)
        with self._lock:
            self.seen.append(page)
        return RedactionOutcome(redacted_text=f"REDACTED PAGE {page}",
                                items_redacted=1, categories=["person_name"])


def test_pages_are_assembled_in_source_order_regardless_of_completion_order(
        monkeypatch, tmp_path):
    captured = _stub_multi_page(monkeypatch, tmp_path, 6, lambda p: f"PAGE {p} X")
    rd.redact_document("CASE_009", "DOC_001", "document-pipeline", "RUN_1",
                       _OrderScramblingRedactor(6), max_workers=4)
    body = captured["redacted"]
    order = [int(seg.split("page=")[1].split(">>>")[0])
             for seg in body.split("<<<PAGE ")[1:]]
    assert order == [1, 2, 3, 4, 5, 6]
    for page in range(1, 7):
        assert f"<<<PAGE page={page}>>>\nREDACTED PAGE {page}" in body, (
            f"page {page} text filed under the wrong marker")


def test_each_page_is_redacted_exactly_once(monkeypatch, tmp_path):
    _stub_multi_page(monkeypatch, tmp_path, 8, lambda p: f"PAGE {p} X")
    redactor = _OrderScramblingRedactor(8)
    rd.redact_document("CASE_009", "DOC_001", "document-pipeline", "RUN_1",
                       redactor, max_workers=4)
    assert sorted(redactor.seen) == list(range(1, 9))


def test_parallel_output_is_byte_identical_to_sequential(monkeypatch, tmp_path):
    """The Go/No-Go criterion: parallelism must not change what is written."""
    seq = _stub_multi_page(monkeypatch, tmp_path, 5, lambda p: f"PAGE {p} X")
    rd.redact_document("CASE_009", "DOC_001", "document-pipeline", "RUN_1",
                       _OrderScramblingRedactor(5), max_workers=1, resume=False)
    sequential = seq["redacted"]

    par = _stub_multi_page(monkeypatch, tmp_path, 5, lambda p: f"PAGE {p} X")
    rd.redact_document("CASE_009", "DOC_001", "document-pipeline", "RUN_1",
                       _OrderScramblingRedactor(5), max_workers=4, resume=False)
    assert par["redacted"] == sequential


def test_observed_concurrency_reaches_the_worker_count(monkeypatch, tmp_path):
    _stub_multi_page(monkeypatch, tmp_path, 8, lambda p: f"PAGE {p} X")
    barrier = threading.Barrier(4, timeout=10)
    lock = threading.Lock()
    active = {"n": 0}
    peak = {"n": 0}

    class _Barriered:
        method = "llm_span_redaction"
        label = "llm_span_redaction:fixture:barriered"

        def redact_page(self, text):
            with lock:
                active["n"] += 1
                peak["n"] = max(peak["n"], active["n"])
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
            with lock:
                active["n"] -= 1
            return RedactionOutcome(redacted_text="x", items_redacted=0,
                                    categories=[])

    rd.redact_document("CASE_009", "DOC_001", "document-pipeline", "RUN_1",
                       _Barriered(), max_workers=4, resume=False)
    assert peak["n"] == 4


# ------------------------------------------------- leak semantics under load --

class _LeakOnPage:
    """Leaks on one specific page; every other page succeeds."""

    method = "llm_span_redaction"
    label = "llm_span_redaction:fixture:leaky"

    def __init__(self, leak_page):
        self.leak_page = leak_page
        self.completed = []
        self._lock = threading.Lock()

    def redact_page(self, text):
        page = _page_of(text)
        if page == self.leak_page:
            raise RedactionLeakError(f"possible leak on page {page}")
        with self._lock:
            self.completed.append(page)
        return RedactionOutcome(redacted_text=f"REDACTED {page}",
                                items_redacted=1, categories=["person_name"])


def test_one_leaking_page_blocks_the_whole_document_and_writes_nothing(
        monkeypatch, tmp_path):
    """The constraint the plan calls decisive: other workers finishing must not
    let a leaking document be recorded as a successful one."""
    captured = _stub_multi_page(monkeypatch, tmp_path, 8, lambda p: f"PAGE {p} X")
    with pytest.raises(RedactionLeakError):
        rd.redact_document("CASE_009", "DOC_001", "document-pipeline", "RUN_1",
                           _LeakOnPage(3), max_workers=4)
    assert "redacted" not in captured, "a leaking document must write no redacted text"
    assert "contract" not in captured, "a leaking document must write no contract"


def test_a_leak_still_preserves_completed_pages_in_the_cache(monkeypatch, tmp_path):
    """Drain-before-reraise exists so finished work is not thrown away. The
    document still fails; the pages that succeeded stay cached so a rerun does
    not re-pay for them."""
    _stub_multi_page(monkeypatch, tmp_path, 8, lambda p: f"PAGE {p} X")
    with pytest.raises(RedactionLeakError):
        rd.redact_document("CASE_009", "DOC_001", "document-pipeline", "RUN_1",
                           _LeakOnPage(3), max_workers=4)
    cache_dir = rd._resume_cache_dir("CASE_009", "DOC_001")
    cached = sorted(p.name for p in cache_dir.glob("page_*.json"))
    assert cached, "completed pages should survive in the cache"
    assert "page_003.json" not in cached, "the leaking page must never be cached"


def test_a_leak_on_the_first_page_blocks_just_as_surely(monkeypatch, tmp_path):
    captured = _stub_multi_page(monkeypatch, tmp_path, 8, lambda p: f"PAGE {p} X")
    with pytest.raises(RedactionLeakError):
        rd.redact_document("CASE_009", "DOC_001", "document-pipeline", "RUN_1",
                           _LeakOnPage(1), max_workers=4)
    assert "redacted" not in captured


def test_workers_one_is_the_sequential_path(monkeypatch, tmp_path):
    captured = _stub_multi_page(monkeypatch, tmp_path, 4, lambda p: f"PAGE {p} X")
    redactor = _OrderScramblingRedactor(4)
    rd.redact_document("CASE_009", "DOC_001", "document-pipeline", "RUN_1",
                       redactor, max_workers=1)
    assert redactor.seen == [1, 2, 3, 4]  # strictly in order
    assert captured["contract"]["items_redacted"] == 4


# ------------------------------------------------------------ worker config --

# A malformed or non-positive env value must fall back to the default rather
# than raise or resolve to zero workers. Written against the CONSTANT, not a
# literal: the point is the fallback behaviour, and a hardcoded number here
# breaks every time the measured default is retuned (it did, 4 -> 12 on
# 2026-08-12) while testing nothing extra. The default's own value is pinned
# deliberately, and separately, in test_default_redact_workers_match_ocr.
@pytest.mark.parametrize("raw,expected", [
    ("", None), ("2", 2), ("0", None), ("-3", None), ("abc", None),
])
def test_worker_count_resolution_falls_back_safely(monkeypatch, raw, expected):
    monkeypatch.setenv(rd.REDACT_WORKERS_ENV, raw)
    want = rd.DEFAULT_REDACT_WORKERS if expected is None else expected
    assert rd._resolve_workers(None) == want


def test_redact_workers_sit_at_the_short_call_knee():
    """Redaction runs at the SHORT-call width, which is not OCR's width.

    An earlier version of this test asserted equality with
    DEFAULT_OCR_WORKERS, written when both had just been set to 12. That was
    wrong as a permanent rule: the two ops have different measured optima
    because they are different workloads. A redact_text call is ~4.1-6.4s of
    which ~4.34s is process spawn, so it saturates early (trivial-call curve
    peaks at 12); a real OCR call waits ~23s on the model with the CPU idle
    and does not contend until ~24 (34-page curve: 12 -> 75.2s, 24 -> 54.6s
    with latency flat, 34 -> 53.0s with latency +5.2s).

    What must NOT come back is the original defect: this constant sat at 4
    with a comment claiming "Matches ocr_extract's default" while OCR was at
    16 -- a prose claim of equality that had silently stopped being true.
    Measured cost on CASE_911: the same 34 scanned pages cleared OCR in
    35-42s per document, then took 25.0s (DOC_002, 15p) and 32.7s (DOC_005,
    19p) in redaction at 4.

    So this pins the VALUE to its own measurement, and pins the invariant
    that actually matters -- redaction must never silently fall back to a
    small default while the rest of the pipeline scales up.
    """
    import llm_providers as lp
    assert rd.DEFAULT_REDACT_WORKERS == 12, (
        "redaction page workers must sit at the short-call knee (12); see the "
        "trivial-call curve in llm_providers.DEFAULT_LLM_MAX_INFLIGHT"
    )
    assert rd.DEFAULT_REDACT_WORKERS <= lp.DEFAULT_LLM_MAX_INFLIGHT, (
        "a per-op width above the process-wide cap would queue every call and "
        "make the op's own measured width unreachable"
    )


def test_explicit_worker_argument_wins_over_env(monkeypatch):
    monkeypatch.setenv(rd.REDACT_WORKERS_ENV, "8")
    assert rd._resolve_workers(2) == 2
