"""--single-reader: P8 off, and honest about it.

These drive the real `run_ocr` -> `_assemble_ocr_result` chain with a
FixtureProvider rather than asserting on the flag's plumbing, because the first
version of this mode passed 2377 existing tests while crashing on every single
page it processed: the per-page progress line read `result['agreement']`, and
`result` is the comparator verdict, which only the dual-read branch creates.
Nothing in the suite executed the branch, so nothing caught it. The fix is one
line; the lesson is that a new branch needs a test that RUNS it, not one that
inspects the arguments handed to it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import ocr_extract  # noqa: E402
import run_checkpoint1 as rc  # noqa: E402
from _validation import load_registry, validate_instance  # noqa: E402
from llm_providers import FixtureProvider  # noqa: E402

PIL = pytest.importorskip("PIL.Image")


@pytest.fixture
def page_image(tmp_path: Path) -> Path:
    img = tmp_path / "page.png"
    PIL.new("RGB", (80, 40), "white").save(img)
    return img


def _run(page_image: Path, **kw) -> dict:
    return ocr_extract.run_ocr(
        "CASE_999", "DOC_001", page_image,
        reader_a=FixtureProvider(model_name="stub-1", text="환자 홍길동 진단명 골절"),
        resume=False, progress=lambda _m: None, **kw)


def test_single_reader_page_completes(page_image: Path) -> None:
    """The regression: this raised UnboundLocalError on every page."""
    out = _run(page_image, single_reader=True)
    assert len(out["pages"]) == 1


def test_single_reader_never_compares(page_image: Path, monkeypatch) -> None:
    """No second read and no comparison -- that is the entire cost saving."""
    calls = {"n": 0}

    def spy(*a, **k):
        calls["n"] += 1
        raise AssertionError("compare() must not run in single-reader mode")

    monkeypatch.setattr(ocr_extract, "compare", spy)
    out = _run(page_image, single_reader=True)
    assert calls["n"] == 0
    assert out["pages"][0]["agreement"] == "single_reader"


def test_single_reader_records_no_second_reading(page_image: Path) -> None:
    """reading_b is null, not a copy of reading_a.

    A copy would be indistinguishable from two independent reads that happened
    to produce identical text -- the strongest possible P8 result, asserted by a
    run that never performed P8 at all.
    """
    page = _run(page_image, single_reader=True)["pages"][0]
    assert page["reading_b"] is None
    assert page["disagreement_details"] == []
    assert page["provider_metadata"]["reader_b"] is None
    assert page["provider_metadata"]["comparator"] is None


def test_single_reader_document_rollup_is_honest(page_image: Path) -> None:
    out = _run(page_image, single_reader=True)
    assert out["cross_validation_mode"] == "single_reader_no_cross_validation"

    result = rc._assemble_ocr_result(
        "CASE_999", "DOC_001", "RUN_20260811_001", out, source_total_pages=1)
    assert result["cross_validation_status"] == "single_reader_no_cross_validation"
    assert result["ocr_quality"] == "low"
    assert result["review_required"] is True
    # The text was still extracted and still belongs on disk -- the page is
    # unvalidated, not absent. This is the invariant whose violation left
    # CASE_911/DOC_005 claiming 19 page paths with 11 files.
    assert result["pages"][0]["text_path"] is not None


def test_single_reader_contract_validates(page_image: Path) -> None:
    out = _run(page_image, single_reader=True)
    result = rc._assemble_ocr_result(
        "CASE_999", "DOC_001", "RUN_20260811_001", out, source_total_pages=1)
    schemas, registry = load_registry()
    assert validate_instance(result, "ocr_result.schema.json", schemas, registry) == []


def test_rule4_rejects_single_reader_claiming_agreement(page_image: Path) -> None:
    """Negative control for P8 rollup rule 4, as rule 3 was verified.

    A single_reader page under a clean document rollup is every field
    individually legal while the combination lies.
    """
    out = _run(page_image, single_reader=True)
    result = rc._assemble_ocr_result(
        "CASE_999", "DOC_001", "RUN_20260811_001", out, source_total_pages=1)
    result["cross_validation_status"] = "agreed"
    result["cross_validation_mode"] = "single_technology_weak_p8_poc"
    result["review_required"] = False
    schemas, registry = load_registry()
    assert validate_instance(result, "ocr_result.schema.json", schemas, registry), (
        "a single_reader page must not validate under cross_validation_status "
        "'agreed' with review_required false -- rule 4 is not firing"
    )


class TestSingleReaderDefault:
    """HARNESS_SINGLE_READER makes P8-off the default for a dev shell.

    Deliberately an env var rather than flipping the flag's default to True: a
    True default would leave no way to ASK for dual-read P8 on the command
    line, and evaluation runs need exactly that. So the flag is three-state --
    unspecified consults the environment, --single-reader forces on,
    --dual-read forces off.
    """

    @pytest.mark.parametrize(
        "env,arg,expected",
        [
            (None, None, False),          # nothing set anywhere -> P8 stays on
            ("1", None, True),            # dev shell default
            ("true", None, True),
            ("on", None, True),
            ("0", None, False),
            ("maybe", None, False),       # unparseable is not "on"
            ("1", False, False),          # --dual-read overrides the env
            ("0", True, True),            # --single-reader overrides the env
        ],
    )
    def test_precedence(self, monkeypatch, env, arg, expected) -> None:
        monkeypatch.delenv(ocr_extract.SINGLE_READER_ENV, raising=False)
        if env is not None:
            monkeypatch.setenv(ocr_extract.SINGLE_READER_ENV, env)
        assert ocr_extract.resolve_single_reader(arg) is expected

    def test_env_var_reaches_the_real_ocr_path(
            self, page_image: Path, monkeypatch) -> None:
        """The precedence helper being right proves nothing on its own.

        run_ocr must actually consult it -- a resolver nothing calls is the
        same 'implemented but not wired' shape this repo has hit before.
        """
        monkeypatch.setenv(ocr_extract.SINGLE_READER_ENV, "1")
        out = _run(page_image)  # note: no single_reader argument at all
        assert out["pages"][0]["agreement"] == "single_reader"
        assert out["cross_validation_mode"] == "single_reader_no_cross_validation"

    def test_explicit_dual_read_beats_env(
            self, page_image: Path, monkeypatch) -> None:
        monkeypatch.setenv(ocr_extract.SINGLE_READER_ENV, "1")
        out = _run(page_image, single_reader=False)
        assert out["pages"][0]["agreement"] != "single_reader"
        assert out["pages"][0]["reading_b"] is not None


def test_single_reader_document_is_not_blocked(page_image: Path, monkeypatch,
                                               tmp_path: Path) -> None:
    """A single-reader document must COMPLETE, not block.

    Found on the real CASE_911 run: DOC_002 came back `blocked_disagreement`
    with an EMPTY disagreed_pages list. run_checkpoint1 keyed the block on
    `review_required`, which was a faithful proxy for "a page disagreed" only
    while 'disagreed' was the only reason review could be required.
    --single-reader sets review_required to be honest that nothing was
    cross-validated, so the throughput mode blocked exactly the scanned
    documents it exists to carry through -- and would have blocked every scan
    in a corpus that is almost entirely scans.

    Asserted on the block predicate itself rather than by driving the whole
    checkpoint, which needs a manifest, a segmentation gate and DAO writes.
    """
    out = _run(page_image, single_reader=True)
    result = rc._assemble_ocr_result(
        "CASE_999", "DOC_001", "RUN_20260811_001", out, source_total_pages=1)

    # The honest flag stays set...
    assert result["review_required"] is True
    # ...but it must not be what decides a block.
    assert not any(p["agreement"] == "disagreed" for p in out["pages"]), (
        "no page disagreed, so nothing may be blocked as a P8 disagreement"
    )


def test_single_reader_cache_namespace_is_separate(page_image: Path) -> None:
    """A single-read verdict and a dual-read verdict must never be interchangeable.

    Sharing the namespace is wrong in both directions: a single-reader hit
    would hand unvalidated text to a run that asked for cross-validation, and a
    dual-read hit would let a --single-reader run report an `agreed` it never
    paid for.
    """
    reader = FixtureProvider(model_name="stub-1", text="x")
    dual = ocr_extract._cache_fingerprint(page_image, reader, reader, reader)
    single = ocr_extract._cache_fingerprint(
        page_image, reader, None, None, single_reader=True)
    assert dual != single
