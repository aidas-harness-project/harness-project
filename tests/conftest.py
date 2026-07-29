"""Shared fixtures. Every dao.py filesystem test runs against a tmp_path,
never the real outputs/ or data/ -- see isolated_dao below.

schemas/ is NOT faked -- tests validate against the project's real schema
files, since that's the actual contract being tested.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import pytest

import dao
import llm_providers
import policy_polarity_semantics
import policy_uid_resolver


@pytest.fixture
def isolated_dao(tmp_path, monkeypatch):
    """Points dao.py's module-level OUTPUTS/DATA at a tmp dir for this test.

    dao's cmd_* functions read these as globals at call time (not bound at
    import time), so monkeypatching the module attributes redirects every
    case_dir()/processed_dir() call without touching the real project tree.
    """
    monkeypatch.setattr(dao, "OUTPUTS", tmp_path / "outputs")
    monkeypatch.setattr(dao, "DATA", tmp_path / "data")
    return tmp_path


@pytest.fixture
def case_id():
    return "CASE_009"


@pytest.fixture
def run_id():
    return "RUN_20260712_001"


@pytest.fixture
def canonicalize():
    """Drive a seeded document through the real migration flow to canonical_v1.

    P0-3 makes canonical_v1 the only state new policy work may be written in,
    so any test that exercises a policy-layer write path needs its document
    genuinely registered. This runs the actual DAO commands -- register the
    source text, hash the raw file, activate -- rather than hand-writing a
    revision index, so a fixture cannot reach a state the production path
    cannot. The same helper is what P0-3's own tests use, so a fixture and the
    validator can never agree on a state neither could really produce.

    Requires the case's manifest to already name the document with a
    `file_path` under `data/raw/`, and that raw file to exist.
    """
    from pathlib import Path

    def _canonicalize(make_args, isolated_dao, case_id, doc_id, text,
                      held_by="document-pipeline", run_id="RUN_20260728_001"):
        text_file = Path(isolated_dao) / f"_canonicalize_{case_id}_{doc_id}.md"
        text_file.write_text(text, encoding="utf-8")
        assert dao.cmd_write_redacted_text(make_args(
            case_id=case_id, doc_id=doc_id, text_file=str(text_file),
            held_by=held_by, run_id=run_id)) == 0
        assert dao.cmd_record_source_digest(make_args(
            case_id=case_id, doc_id=doc_id, held_by=held_by,
            run_id=run_id, expect=None)) == 0
        assert dao.cmd_enable_canonical_uids(make_args(
            case_id=case_id, doc_id=doc_id, held_by=held_by,
            run_id=run_id)) == 0
        assert dao.uid_scheme_for(case_id, doc_id) == "canonical_v1"

    return _canonicalize


@pytest.fixture
def issue_semantic_receipts(isolated_dao, make_args, monkeypatch):
    """Issue real DAO receipts for unrelated canonical policy test fixtures.

    Tests whose subject is UID/evidence/table behavior still have to satisfy
    the production semantic gate. They use a mocked provider but the real
    command, source-span resolution, protected index, schema, and receipt hash;
    no test can regain the old self-declared-polarity path.
    """
    counter = 0
    # Derived from the production table rather than restated, so a change to
    # the bucket contract cannot leave these fixtures asserting the old one.
    expected_by_bucket = policy_polarity_semantics.BUCKET_REQUIREMENT

    def _trusted(provider):
        def _resolve(env=None):
            return dao.TrustedAnalyzer(
                provider_name=provider.provider_name,
                model_name=provider.model_name,
                identity={
                    "provider": provider.provider_name,
                    "model": provider.model_name,
                    "source_prompt_version":
                        policy_polarity_semantics.SOURCE_PROMPT_VERSION,
                    "comparison_prompt_version":
                        policy_polarity_semantics.COMPARISON_PROMPT_VERSION,
                    "semantic_schema_version":
                        policy_polarity_semantics.SEMANTIC_SCHEMA_VERSION,
                    "settings_fingerprint":
                        policy_polarity_semantics.settings_fingerprint(),
                },
            )
        return _resolve

    def _issue(contract, case_id, doc_id):
        nonlocal counter
        context, errors = dao._uid_source_context(case_id, doc_id)
        assert not errors
        for clause in contract.get("clauses") or []:
            for bucket, classification in expected_by_bucket.items():
                for condition in clause.get(bucket) or []:
                    records = policy_uid_resolver.resolve_spans(
                        context, condition.get("source_span_uids"),
                        "semantic test fixture")
                    records = sorted(
                        records,
                        key=lambda item: (
                            item["physical_page"], item["start_char"],
                            item["end_char"], item["uid"]))
                    passage = policy_polarity_semantics.source_passage(records)
                    source_response = {
                        "source_classification": classification,
                        "target_predicates": ["fixture"],
                        "negation_scope_analysis":
                            "mocked semantic fixture for an unrelated gate",
                        "propositions": [{
                            "text": passage,
                            "classification": classification,
                            "source_start": 0,
                            "source_end": len(passage),
                            "reason": "fixture classification",
                        }],
                        "review_required": False,
                    }
                    comparison_response = {
                        "meaning_preserved": True,
                        "omitted_propositions": [],
                        "added_propositions": [],
                        "contradiction_detected": False,
                        "review_required": False,
                    }

                    class _TwoPhase(llm_providers.FixtureProvider):
                        """Answers Phase A then Phase B off prompt_version."""

                        def compare_text(self, prompt, prompt_version):
                            payload = (
                                source_response
                                if prompt_version == policy_polarity_semantics
                                .SOURCE_PROMPT_VERSION
                                else comparison_response)
                            return self._result(
                                json.dumps(payload, ensure_ascii=False),
                                prompt_version, {})

                    provider = _TwoPhase(model_name="semantic-fixture-v1")
                    monkeypatch.setattr(
                        dao.llm_providers, "build_provider",
                        lambda *args, _provider=provider, **kwargs: _provider)
                    # The analyzer is deployment-owned in production; tests
                    # inject one, which is the only sanctioned way to reach a
                    # fixture provider at all.
                    monkeypatch.setattr(
                        dao, "resolve_trusted_analyzer", _trusted(provider))
                    counter += 1
                    args = make_args(
                        case_id=case_id,
                        doc_id=doc_id,
                        source_span_uid=[item["uid"] for item in records],
                        source_selector_json=json.dumps(
                            {"source_spans": condition["source_span_uids"]},
                            ensure_ascii=False),
                        condition_text=condition["text"],
                        bucket=bucket,
                        held_by="policy-pipeline",
                        run_id=contract.get("run_id")
                        or "RUN_20260728_001",
                    )
                    assert dao.cmd_analyze_policy_polarity(args) == 0
                    condition_hash = policy_polarity_semantics.sha256_text(
                        condition["text"])
                    # No bucket filter: a receipt is not issued for a bucket
                    # any more. Source spans + condition bytes identify it.
                    receipt = next(
                        item for item in
                        dao.load_policy_polarity_semantic_index(
                            case_id)["receipts"]
                        if item["condition_text_sha256"] == condition_hash
                        and item["source_span_uids"] == [
                            record["uid"] for record in records])
                    condition["polarity_analysis_receipt_id"] = \
                        receipt["receipt_id"]
        return contract

    return _issue


@pytest.fixture
def segment_pdf():
    """Build a real PDF whose printed page numbers are genuinely on the pages.

    P0-6's whole point is that the DAO reads the parent PDF itself, so a test
    that stubs the read proves nothing about the gate. Every P0-6 test therefore
    runs against an actual pymupdf-rendered file.

    `body_for(logical)` supplies each page's text; the printed marker
    'N / TOTAL' is appended for the pages that should carry one.
    `unnumbered` lists logical pages deliberately printed WITHOUT a marker (the
    "no printed number" case, which must block rather than fall back to the
    offset).
    """
    import fitz

    def _build(path, *, total_logical, offset=7, body_for=None,
               unnumbered=(), front_matter_body="표지"):
        """Physical page = logical + offset. The first `offset` physical pages
        are unnumbered front matter, exactly like CASE_030's real policy PDF."""
        # ASCII keeps the generated PDF's embedded-text layer deterministic
        # with PyMuPDF's built-in test font. Tests for Korean extraction live
        # at the OCR/document layer; these fixtures test provenance identity.
        body_for = body_for or (lambda lp: f"Clause {lp} body")
        doc = fitz.open()
        for _ in range(offset):
            page = doc.new_page()
            page.insert_text((72, 72), front_matter_body, fontsize=11)
        for logical in range(1, total_logical + 1):
            page = doc.new_page()
            page.insert_text((72, 72), body_for(logical), fontsize=11)
            if logical not in unnumbered:
                page.insert_text(
                    (72, 700), f"{logical} / {total_logical}", fontsize=9)
        doc.save(str(path))
        doc.close()
        return path

    return _build


@pytest.fixture
def register_derivation():
    """Run the real `register-segment-derivation` command.

    Tests use this rather than hand-writing a page_map, because hand-writing one
    is exactly what P0-6 makes impossible -- a fixture that could do it would be
    testing a path production cannot reach.
    """
    def _register(make_args, case_id, doc_id, pages, page_offset,
                  held_by="document-pipeline", run_id="RUN_20260728_001",
                  parent_document_id=None, page_map_file=None,
                  expect_parent_sha256=None):
        return dao.cmd_register_segment_derivation(make_args(
            case_id=case_id, doc_id=doc_id, pages=pages,
            page_offset=page_offset, held_by=held_by, run_id=run_id,
            parent_document_id=parent_document_id,
            page_map_file=page_map_file,
            expect_parent_sha256=expect_parent_sha256))

    return _register


@pytest.fixture
def make_args():
    """Builds an argparse.Namespace-like object for calling dao's cmd_*
    functions directly, without shelling out. Pass only the overrides a
    given test cares about; everything else defaults to None so a cmd_*
    function that ignores an unused attribute doesn't need it stubbed.
    """
    from types import SimpleNamespace

    def _make(**overrides):
        defaults = dict(
            case_id="CASE_009", doc_id="DOC_001", run_id="RUN_20260712_001",
            held_by="test-agent", purpose=None, stage=None,
            filename=None, data_file=None, schema_name=None,
            page=None, text_file=None, file_name=None, status=None,
            reviewer=None, reason=None, doc_path=None,
            topic=None, sources_file=None, conflict_id=None, verdict=None, note=None,
            caller_stage=None, description=None, version=None, fields_file=None,
            expect=None, pages=None, page_offset=None, page_map_file=None,
            parent_document_id=None, expect_parent_sha256=None,
            artifact_kind=None, artifact_id=None, target_key=None,
            decision=None, document_id=None,
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    return _make
