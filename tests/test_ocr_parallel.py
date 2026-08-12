"""run_ocr's page-level parallelism.

Every test here pins a property that the SEQUENTIAL loop gave away for free
and that concurrency can silently take back. They are written to pass against
the sequential implementation too -- that is the point: they are the contract,
not a description of the new code. A parallel run must be indistinguishable
from a sequential one in its output, and differ only in wall time.

Provider calls are faked throughout, keyed by image path rather than by call
order, because a positional fake (FakeReader's readings.pop(0)) cannot express
"page 3 got page 3's text" once calls are interleaved.
"""
import json
import threading
import time
from pathlib import Path

import pytest

import ocr_extract as oe
from llm_providers import ProviderResult


def _page_number(image_path: Path) -> int:
    """page_007.png -> 7. The fakes key off this, so a mixed-up image/page
    association is a test failure rather than something that averages out."""
    return int(Path(image_path).stem.split("_")[-1])


class PathKeyedReader:
    """Returns text derived from the image it was actually handed, and records
    calls under a lock so the recording itself is not the race."""

    def __init__(self, tag, *, delay=0.0, fail_on=()):
        self.provider_name = "fixture"
        self.model_name = f"fixture-{tag}"
        self.tag = tag
        self.delay = delay
        self.fail_on = set(fail_on)
        self.calls = []
        self.max_concurrent = 0
        self._active = 0
        self._lock = threading.Lock()

    def transcribe_image(self, image_path, prompt, prompt_version):
        page = _page_number(image_path)
        with self._lock:
            self._active += 1
            self.max_concurrent = max(self.max_concurrent, self._active)
            self.calls.append(page)
        try:
            if self.delay:
                time.sleep(self.delay)
            if page in self.fail_on:
                raise RuntimeError(f"{self.tag} failed on page {page}")
            return ProviderResult(
                self.provider_name, self.model_name, prompt_version, f"{self.tag}{page}"
            )
        finally:
            with self._lock:
                self._active -= 1


class CountingComparator:
    def __init__(self, verdict="AGREE"):
        self.provider_name = "fixture"
        self.model_name = "fixture-comparator"
        self.verdict = verdict
        self.calls = 0
        self._lock = threading.Lock()

    def compare_text(self, prompt, prompt_version):
        with self._lock:
            self.calls += 1
        return ProviderResult(
            self.provider_name, self.model_name, prompt_version, self.verdict
        )


def _patch_split(monkeypatch, tmp_path, page_count):
    def fake_split(doc_path, out_dir, max_pages=None, dpi=None):
        imgs = []
        for n in range(1, page_count + 1):
            p = out_dir / f"page_{n:03d}.png"
            p.write_bytes(b"img")
            imgs.append(p)
        return imgs

    monkeypatch.setattr(oe, "split_to_page_images", fake_split)
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-1.4 fake")
    return doc


@pytest.fixture
def scratch(monkeypatch, tmp_path):
    monkeypatch.setattr(oe, "SCRATCH_ROOT", tmp_path / "_ocr_scratch")
    return tmp_path


def test_pages_come_back_in_source_order_regardless_of_completion_order(scratch, monkeypatch):
    """The single most important property. Pages must be ordered by page
    number, not by which provider call happened to return first -- downstream,
    `pages[i]` is written as page i+1's text, so a reordering here silently
    files page 9's text under page 3."""
    doc = _patch_split(monkeypatch, scratch, 8)

    # Make later pages finish FIRST: page 1 is slowest, page 8 fastest. Under a
    # naive "append as completed" implementation this inverts the list.
    class DescendingDelayReader(PathKeyedReader):
        def transcribe_image(self, image_path, prompt, prompt_version):
            self.delay = 0.02 * (9 - _page_number(image_path))
            return super().transcribe_image(image_path, prompt, prompt_version)

    result = oe.run_ocr(
        "CASE_900", "DOC_001", doc,
        reader_a=DescendingDelayReader("A"),
        reader_b=DescendingDelayReader("B"),
        comparator=CountingComparator(),
    )

    assert [p["page"] for p in result["pages"]] == list(range(1, 9))
    # And each page carries ITS OWN text, not a neighbour's.
    for p in result["pages"]:
        assert p["reading_a"] == f"A{p['page']}"
        assert p["reading_b"] == f"B{p['page']}"


def test_each_page_is_read_exactly_once(scratch, monkeypatch):
    """A page read twice is money spent twice and, worse, evidence that the
    work queue can hand the same unit out to two workers."""
    doc = _patch_split(monkeypatch, scratch, 10)
    reader_a = PathKeyedReader("A", delay=0.005)
    reader_b = PathKeyedReader("B", delay=0.005)

    oe.run_ocr("CASE_900", "DOC_001", doc,
               reader_a=reader_a, reader_b=reader_b, comparator=CountingComparator())

    assert sorted(reader_a.calls) == list(range(1, 11))
    assert sorted(reader_b.calls) == list(range(1, 11))


def test_reader_a_and_reader_b_see_the_same_image_for_a_given_page(scratch, monkeypatch):
    """P8's whole premise is two independent reads OF THE SAME PAGE. If
    concurrency ever paired reader_a's page 4 with reader_b's page 5, compare()
    would be adjudicating two different pages and every verdict would be
    meaningless."""
    doc = _patch_split(monkeypatch, scratch, 6)

    result = oe.run_ocr("CASE_900", "DOC_001", doc,
                        reader_a=PathKeyedReader("A", delay=0.01),
                        reader_b=PathKeyedReader("B"),
                        comparator=CountingComparator())

    for p in result["pages"]:
        assert p["reading_a"].lstrip("A") == p["reading_b"].lstrip("B") == str(p["page"])


def test_a_failing_page_does_not_lose_the_pages_that_succeeded(scratch, monkeypatch):
    """Sequential runs cached each page as it completed, so a crash on page 7
    kept pages 1-6. That must survive parallelism: the resume cache is the only
    thing standing between an interruption and re-paying for the whole
    document."""
    doc = _patch_split(monkeypatch, scratch, 6)

    with pytest.raises(Exception):
        oe.run_ocr("CASE_900", "DOC_001", doc,
                   reader_a=PathKeyedReader("A", fail_on=[4]),
                   reader_b=PathKeyedReader("B"),
                   comparator=CountingComparator())

    cache = oe._resume_cache_dir("CASE_900", "DOC_001")
    cached_pages = sorted(int(p.stem.split("_")[-1]) for p in cache.glob("page_*.json"))
    # Page 4 failed, so it must NOT be cached. Some subset of the others will
    # have completed; asserting an exact set would be asserting a scheduling
    # order. What matters is that work was preserved and the failure was not.
    assert 4 not in cached_pages
    assert cached_pages, "a mid-document failure discarded every completed page"


def test_resume_still_skips_cached_pages_and_reads_only_the_rest(scratch, monkeypatch):
    """The cache-hit path must stay cheap under parallelism -- a cached page
    costs zero provider calls, exactly as before."""
    doc = _patch_split(monkeypatch, scratch, 5)
    cache = oe._resume_cache_dir("CASE_900", "DOC_001")

    reader_a = PathKeyedReader("A")
    reader_b = PathKeyedReader("B")
    comparator = CountingComparator()

    # Seed under the fingerprint this run will actually compute. Since
    # 2026-08-11 an entry is only a hit for the exact dpi/provider/model/prompt
    # that produced it, so seeding without one (as this test used to) is a
    # miss -- correctly, but it would no longer be testing the cache-hit path.
    # Every fake page shares the same bytes, so one fingerprint covers all.
    seeded_image = scratch / "seed.png"
    seeded_image.write_bytes(b"img")
    fingerprint = oe._cache_fingerprint(seeded_image, reader_a, reader_b, comparator)
    for n in (1, 3):
        oe._save_cached_page(cache, n, {
            "page": n, "reading_a": f"CACHED{n}", "reading_b": f"CACHED{n}",
            "agreement": "agreed", "disagreement_details": [], "provider_metadata": {},
        }, fingerprint)

    result = oe.run_ocr("CASE_900", "DOC_001", doc,
                        reader_a=reader_a, reader_b=reader_b,
                        comparator=comparator)

    assert sorted(reader_a.calls) == [2, 4, 5]
    assert [p["page"] for p in result["pages"]] == [1, 2, 3, 4, 5]
    assert result["pages"][0]["reading_a"] == "CACHED1"
    assert result["pages"][2]["reading_a"] == "CACHED3"


def test_disagreements_are_attributed_to_the_right_page(scratch, monkeypatch):
    """A verdict landing on the wrong page is the quiet version of the
    ordering bug: the run halts on a page that actually agreed, while a real
    disagreement passes as agreed."""
    doc = _patch_split(monkeypatch, scratch, 5)

    class PerPageComparator(CountingComparator):
        def compare_text(self, prompt, prompt_version):
            with self._lock:
                self.calls += 1
            # Page 3's readings are "A3"/"B3"; disagree only on that pair.
            verdict = "DISAGREE" if ("A3" in prompt and "B3" in prompt) else "AGREE"
            return ProviderResult(self.provider_name, self.model_name, prompt_version, verdict)

    result = oe.run_ocr("CASE_900", "DOC_001", doc,
                        reader_a=PathKeyedReader("A", delay=0.01),
                        reader_b=PathKeyedReader("B"),
                        comparator=PerPageComparator())

    disagreed = [p["page"] for p in result["pages"] if p["agreement"] == "disagreed"]
    assert disagreed == [3]


def test_provider_metadata_stays_with_its_own_page(scratch, monkeypatch):
    doc = _patch_split(monkeypatch, scratch, 4)
    result = oe.run_ocr("CASE_900", "DOC_001", doc,
                        reader_a=PathKeyedReader("A", delay=0.01),
                        reader_b=PathKeyedReader("B"),
                        comparator=CountingComparator())

    for p in result["pages"]:
        assert p["provider_metadata"]["reader_a"]["model_name"] == "fixture-A"
        assert p["provider_metadata"]["reader_b"]["model_name"] == "fixture-B"


def test_single_page_document_is_unchanged(scratch, monkeypatch):
    """Guard against the parallel path being a different code path for the
    trivial case -- most documents in the corpus are short."""
    doc = _patch_split(monkeypatch, scratch, 1)
    result = oe.run_ocr("CASE_900", "DOC_001", doc,
                        reader_a=PathKeyedReader("A"), reader_b=PathKeyedReader("B"),
                        comparator=CountingComparator())

    assert [p["page"] for p in result["pages"]] == [1]
    assert result["pages"][0]["reading_a"] == "A1"


def test_progress_is_reported_once_per_page(scratch, monkeypatch):
    """progress() is called from worker threads once parallel; it must still
    fire exactly once per page and not be dropped or duplicated."""
    doc = _patch_split(monkeypatch, scratch, 6)
    lock = threading.Lock()
    messages = []

    def progress(msg):
        with lock:
            messages.append(msg)

    oe.run_ocr("CASE_900", "DOC_001", doc,
               reader_a=PathKeyedReader("A", delay=0.005),
               reader_b=PathKeyedReader("B"),
               comparator=CountingComparator(), progress=progress)

    assert len(messages) == 6


# --------------------------------------------- cache fingerprint (2026-08-11) --

def test_a_different_dpi_is_a_cache_miss_not_a_stale_p8_verdict(scratch, monkeypatch):
    """The resume cache was keyed on case/doc/page ALONE until 2026-08-11.

    That made a re-render at a different dpi a cache HIT on the old result, so
    a 400-dpi run would have been served the 200-dpi verdict and reported the
    same P8 agreement -- making a resolution experiment structurally incapable
    of measuring anything. Worse than a wasted experiment: the cached value is
    a P8 AGREEMENT decision, the gate every downstream stage trusts, so a
    stale hit asserts a page pair agreed under settings it was never read at.
    """
    doc = _patch_split(monkeypatch, scratch, 3)
    cache = oe._resume_cache_dir("CASE_900", "DOC_001")

    reader_a = PathKeyedReader("A")
    reader_b = PathKeyedReader("B")
    comparator = CountingComparator()

    seeded = scratch / "seed.png"
    seeded.write_bytes(b"img")
    fp_200 = oe._cache_fingerprint(seeded, reader_a, reader_b, comparator, 200)
    for n in (1, 2, 3):
        oe._save_cached_page(cache, n, {
            "page": n, "reading_a": "STALE", "reading_b": "STALE",
            "agreement": "agreed", "disagreement_details": [], "provider_metadata": {},
        }, fp_200)

    # Same document, same readers, different render resolution.
    result = oe.run_ocr("CASE_900", "DOC_001", doc,
                        reader_a=reader_a, reader_b=reader_b,
                        comparator=comparator, dpi=400)

    assert sorted(reader_a.calls) == [1, 2, 3], (
        "a dpi change must re-read every page, not reuse the 200-dpi verdict")
    assert all(p["reading_a"] != "STALE" for p in result["pages"]), (
        "a stale cached transcription reached the result")


def test_a_different_reader_model_is_a_cache_miss(scratch, monkeypatch):
    """P8's premise is WHICH two readers agreed, so a verdict cached under one
    model must not be served for another."""
    doc = _patch_split(monkeypatch, scratch, 2)
    cache = oe._resume_cache_dir("CASE_900", "DOC_001")

    seeded = scratch / "seed.png"
    seeded.write_bytes(b"img")

    old_reader = PathKeyedReader("A")
    old_reader.model_name = "old-model"
    fp_old = oe._cache_fingerprint(seeded, old_reader, PathKeyedReader("B"),
                                   CountingComparator())
    for n in (1, 2):
        oe._save_cached_page(cache, n, {
            "page": n, "reading_a": "STALE", "reading_b": "STALE",
            "agreement": "agreed", "disagreement_details": [], "provider_metadata": {},
        }, fp_old)

    new_reader = PathKeyedReader("A")
    new_reader.model_name = "new-model"
    result = oe.run_ocr("CASE_900", "DOC_001", doc,
                        reader_a=new_reader, reader_b=PathKeyedReader("B"),
                        comparator=CountingComparator())

    assert sorted(new_reader.calls) == [1, 2]
    assert all(p["reading_a"] != "STALE" for p in result["pages"])


def test_a_pre_fingerprint_cache_entry_is_a_miss(scratch, monkeypatch):
    """33 such entries existed on disk when fingerprinting was added. They
    cannot be shown to match any current settings, so they must not be trusted
    -- re-reading is the only safe handling."""
    doc = _patch_split(monkeypatch, scratch, 2)
    cache = oe._resume_cache_dir("CASE_900", "DOC_001")
    cache.mkdir(parents=True, exist_ok=True)
    for n in (1, 2):
        (cache / f"page_{n:03d}.json").write_text(json.dumps({
            "page": n, "reading_a": "LEGACY", "reading_b": "LEGACY",
            "agreement": "agreed", "disagreement_details": [], "provider_metadata": {},
        }), encoding="utf-8")

    reader_a = PathKeyedReader("A")
    result = oe.run_ocr("CASE_900", "DOC_001", doc,
                        reader_a=reader_a, reader_b=PathKeyedReader("B"),
                        comparator=CountingComparator())

    assert sorted(reader_a.calls) == [1, 2]
    assert all(p["reading_a"] != "LEGACY" for p in result["pages"])


# ---------------------------------------------- default worker count (T12) --

def test_default_worker_count_is_the_measured_knee(monkeypatch):
    """4 -> 8 -> 16 over 2026-08-11, then 16 -> 12 on measurement 2026-08-12.

    The 4 -> 8 step was measured: 141.32s -> 86.06s on a real 12-page scan, a
    1.64x speedup from this constant alone. The 8 -> 16 step was NOT -- it was
    recorded at the time as a deliberate bet, justified by CASE_953 making 99
    provider calls with zero rate-limit errors.

    That bet lost, on the axis it was not watching. Measured 2026-08-12 with
    24 trivial `claude -p` calls (model work ~0, so this is the spawn curve):

        workers    4      8     12     16     24
        wall     28.7s  23.0s  19.9s  23.6s  32.1s

    Throughput peaks at 12 and degrades above it -- 24 workers is slower than
    4 -- with ZERO rate-limit errors at every width. Each CLI call is a full
    node child process, so the binding constraint is local spawn cost, whose
    symptom is slowness. The zero-rate-limit-errors evidence behind the 16
    bet was structurally blind to it.

    Unlike every previous value here, 12 IS a measured optimum.

    Pinned because the value is load-bearing and invisible -- nothing else in
    the pipeline states it, and a silent revert would show up only as a slower
    run that still passes every other test.
    """
    monkeypatch.delenv("HARNESS_OCR_WORKERS", raising=False)
    assert oe.DEFAULT_OCR_WORKERS == 12
    assert oe._resolve_workers(None) == 12


def test_env_still_overrides_the_default(monkeypatch):
    monkeypatch.setenv("HARNESS_OCR_WORKERS", "3")
    assert oe._resolve_workers(None) == 3


def test_explicit_argument_beats_env(monkeypatch):
    monkeypatch.setenv("HARNESS_OCR_WORKERS", "3")
    assert oe._resolve_workers(5) == 5


def test_one_worker_restores_the_sequential_loop(monkeypatch):
    monkeypatch.delenv("HARNESS_OCR_WORKERS", raising=False)
    assert oe._resolve_workers(1) == 1
    assert oe._resolve_workers(0) == 1
    assert oe._resolve_workers(-4) == 1


def test_eight_workers_still_produce_source_ordered_pages(scratch, monkeypatch):
    """The property that must survive raising the count: more concurrency must
    not reorder pages, since pages[i] is written as page i+1's text."""
    monkeypatch.delenv("HARNESS_OCR_WORKERS", raising=False)
    doc = _patch_split(monkeypatch, scratch, 12)

    class _DescendingDelay(PathKeyedReader):
        def transcribe_image(self, image_path, prompt, prompt_version):
            self.delay = 0.005 * (13 - _page_number(image_path))
            return super().transcribe_image(image_path, prompt, prompt_version)

    result = oe.run_ocr("CASE_900", "DOC_001", doc,
                        reader_a=_DescendingDelay("A"), reader_b=_DescendingDelay("B"),
                        comparator=CountingComparator())

    assert [p["page"] for p in result["pages"]] == list(range(1, 13))
    for p in result["pages"]:
        assert p["reading_a"] == f"A{p['page']}"
