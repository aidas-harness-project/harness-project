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
    def fake_split(doc_path, out_dir, max_pages=None):
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
    for n in (1, 3):
        oe._save_cached_page(cache, n, {
            "page": n, "reading_a": f"CACHED{n}", "reading_b": f"CACHED{n}",
            "agreement": "agreed", "disagreement_details": [], "provider_metadata": {},
        })

    reader_a = PathKeyedReader("A")
    result = oe.run_ocr("CASE_900", "DOC_001", doc,
                        reader_a=reader_a, reader_b=PathKeyedReader("B"),
                        comparator=CountingComparator())

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
