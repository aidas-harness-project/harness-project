"""Conflict candidate ids, followed through the real `run()`.

A unit test that hands the policy linker `{"primary_diagnosis": ["CAC_0001"]}`
proves the linker can attach an id. It cannot prove the driver ever gives it
one -- and it did not. `run()` built the map as
`{field_id: [] for ... if status == "conflict"}`: every conflicting field
present, every list empty. The linker then took the empty list as "no
conflict", and a requirement resting on a fact with two contradictory readings
published `evidence_status: supported` with `conflict_candidate_ids: []` and
the reason "An asserted claim fact states this condition."

Nothing in the result was internally inconsistent, which is why the unit tests
were content: `conflict_candidates` did list the disagreement. The failure was
that the two artifacts were numbered independently, so the place a reviewer
would look to judge the clause showed a clean requirement, and the place that
recorded the dispute was somewhere else entirely.

These tests therefore drive `run()` end to end with the DAO stubbed at its
subprocess edge, and assert on the *published contract* rather than on a
function's arguments.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import claim_analysis_contracts as contracts
import run_claim_analysis_selective as driver


ROOT = Path(__file__).resolve().parent.parent

CASE_ID = "CASE_9401"
RUN_ID = "RUN_20260820_1"

# DOC_001 and DOC_002 disagree on the diagnosis; DOC_900 is the policy.
PAGES = {
    ("DOC_001", 1): "진 단 서\n진단명: 우측 요골 골절\n",
    ("DOC_002", 1): "입퇴원요약\n진단명: 좌측 요골 골절\n",
}
POLICY_PAGES = {("DOC_900", 3): "제3조 진단 담보\n보상하는 손해\n"}

MANIFEST = {"documents": [
    {"document_id": "DOC_001", "document_type": "medical_record",
     "medical_classification": {"status": "deterministic_title",
                                "kind": "diagnosis_certificate"}},
    {"document_id": "DOC_002", "document_type": "medical_record",
     "medical_classification": {"status": "deterministic_title",
                                "kind": "admission_discharge_summary"}},
    {"document_id": "DOC_900", "document_type": "insurance_policy"},
]}

INDEX = {"documents": [{"document_id": "DOC_900", "clauses": [
    {"page": 3, "policy_name": "상해보험 보통약관", "article": "제3조",
     "heading": "진단 담보"},
]}]}


def _bundle(pages) -> dict:
    documents: dict[str, list] = {}
    for (document_id, page), text in pages.items():
        documents.setdefault(document_id, []).append({"page": page, "text": text})
    return {"documents": [{"document_id": document_id, "pages": entries}
                          for document_id, entries in documents.items()]}


class Provider:
    model_name = "stub"


def _install(monkeypatch, *, published: list, index=INDEX, dao_calls=None):
    """Stub the DAO subprocess edge; everything else is the real driver."""
    run_state = {"stages": [{"stage_name": "claim_analysis",
                             "status": "in_progress"}]}

    def fake_json(args, allow_missing=False):
        if dao_calls is not None:
            dao_calls.append(list(args))
        command = args[0]
        if command == "read-contract":
            name = args[2]
            if name == "_run_state.json":
                return run_state
            # The contract's real name, with no leading underscore. The stub
            # spelled it "_document_manifest.json" until 2026-08-20, matching a
            # typo in the driver -- so the test passed while the driver failed
            # on every real case with NOT_FOUND. A stub that mirrors a defect
            # verifies nothing; this one now names what the DAO actually serves.
            if name == "document_manifest.json":
                return MANIFEST
            return None                      # no filing contracts exist
        if command == "read-redacted-text-bundle":
            wanted = {a.split("=", 1)[1] for a in args if a.startswith("--doc-id=")}
            source = POLICY_PAGES if "DOC_900" in wanted else PAGES
            return _bundle({k: v for k, v in source.items() if k[0] in wanted})
        if command == "read-document-index":
            return index
        if command == "policy-snapshot":
            # The real shape `dao.policy_snapshot_for` emits, not an
            # approximation of it -- a stub that differs from the DAO would
            # let a schema-invalid contract pass these tests.
            return {"documents": [{"document_id": "DOC_900",
                                   "digest_sha256": "a" * 64}],
                    "snapshot_sha256": "b" * 64}
        return None

    def fake_write(args):
        name = args[2]
        with open(args[args.index("--data-file") + 1], encoding="utf-8") as handle:
            published.append((name, json.load(handle)))

    monkeypatch.setattr(driver, "_dao_json", fake_json)
    monkeypatch.setattr(driver, "_dao_write", fake_write)

    config = copy.deepcopy(contracts.load_default_routing_config())
    config["behavior_enabled"] = True
    config["activation"] = {
        "approved_by": "test", "authority_role": "손해사정사",
        "approved_at": "2026-08-20T00:00:00Z", "scope": "unit test only"}
    monkeypatch.setattr(driver.routing, "load_routing_config", lambda: config)

    def fake_reader(provider, pages_by_document):
        def extract(document_id, kind, field_rows):
            if callable(pages_by_document):
                pages_by_document(document_id)
            wanted = {row["field_id"] for row in field_rows}
            if "primary_diagnosis" not in wanted:
                return {}
            if document_id == "DOC_001":
                return {"primary_diagnosis": {
                    "value": "우측 요골 골절", "page": 1, "quote": "우측 요골 골절"}}
            if document_id == "DOC_002":
                return {"primary_diagnosis": {
                    "value": "좌측 요골 골절", "page": 1, "quote": "좌측 요골 골절"}}
            return {}
        return extract

    monkeypatch.setattr(driver.extraction, "make_reader", fake_reader)
    return config


def _result(published) -> dict:
    return next(payload for name, payload in published
                if name == driver.RESULT_CONTRACT)


@pytest.fixture
def published(monkeypatch) -> list:
    items: list = []
    _install(monkeypatch, published=items)
    driver.run(case_id=CASE_ID, run_id=RUN_ID, held_by="claim-analysis",
               provider=Provider())
    return items


# ------------------------------------------------ the id exists at all --

def test_the_run_records_the_disagreement(published) -> None:
    """Fixture guard: without a real conflict the rest proves nothing."""
    result = _result(published)
    assert [row["field_id"] for row in result["conflict_candidates"]] == [
        "primary_diagnosis"]
    fact = next(row for row in result["claim_facts"]
                if row["field_id"] == "primary_diagnosis")
    assert fact["resolution_status"] == "conflict"


def test_the_policy_requirement_carries_the_same_id(published) -> None:
    """The defect, stated as an equality between two places in one contract."""
    result = _result(published)
    candidate_id = result["conflict_candidates"][0]["conflict_candidate_id"]

    link = next(link for link in result["policy_links"]
                if link["coverage_id"] == "primary_diagnosis")
    requirement = link["requirements"][0]
    assert requirement["conflict_candidate_ids"] == [candidate_id]


def test_the_requirement_is_marked_conflict_not_supported(published) -> None:
    result = _result(published)
    link = next(link for link in result["policy_links"]
                if link["coverage_id"] == "primary_diagnosis")
    requirement = link["requirements"][0]
    assert requirement["evidence_status"] == "conflict"
    # Not the reason a supported requirement carries, and it names the
    # disagreement. Both reasons are Korean: they print in the report.
    assert "충족한다고" not in requirement["reason"]
    assert "서로 다른 두 기재" in requirement["reason"]


def test_the_claim_fact_and_the_requirement_agree(published) -> None:
    """Same id on both sides of the contract, reachable from either."""
    result = _result(published)
    fact = next(row for row in result["claim_facts"]
                if row["field_id"] == "primary_diagnosis")
    link = next(link for link in result["policy_links"]
                if link["coverage_id"] == "primary_diagnosis")
    assert fact["conflict_candidate_ids"] == \
        link["requirements"][0]["conflict_candidate_ids"]


def test_every_referenced_id_resolves_to_a_declared_candidate(published) -> None:
    """No requirement may point at an id the result never declared."""
    result = _result(published)
    declared = {row["conflict_candidate_id"]
                for row in result["conflict_candidates"]}
    for link in result["policy_links"]:
        for requirement in link["requirements"]:
            for candidate_id in requirement.get("conflict_candidate_ids") or []:
                assert candidate_id in declared


# --------------------------------------------------------- read attribution --

def test_run_fetches_each_selected_document_lazily_and_with_run_id(
    monkeypatch,
) -> None:
    published: list = []
    dao_calls: list[list[str]] = []
    _install(monkeypatch, published=published, dao_calls=dao_calls)

    driver.run(case_id=CASE_ID, run_id=RUN_ID, held_by="claim-analysis",
               provider=Provider())

    reads = [args for args in dao_calls
             if args[0] == "read-redacted-text-bundle"]
    assert reads
    for args in reads:
        document_ids = [arg for arg in args if arg.startswith("--doc-id=")]
        assert len(document_ids) == 1
        assert f"--run-id={RUN_ID}" in args


def test_policy_snapshot_read_is_attributed_to_the_run(monkeypatch) -> None:
    published: list = []
    dao_calls: list[list[str]] = []
    _install(monkeypatch, published=published, dao_calls=dao_calls)

    driver.run(case_id=CASE_ID, run_id=RUN_ID, held_by="claim-analysis",
               provider=Provider())

    snapshot = next(args for args in dao_calls if args[0] == "policy-snapshot")
    assert snapshot[-2:] == ["--run-id", RUN_ID]


# ------------------------------------------------------- single ownership --

def test_ids_are_assigned_once_and_are_deterministic() -> None:
    """Same inputs, same numbering -- and only one place that numbers."""
    def outcome(field_id, status):
        item = driver.FieldExtractionOutcome(
            field_id=field_id, domain_code="diagnosis", grade="B")
        item.status = status
        return item

    outcomes = [outcome("a", "conflict"), outcome("b", "asserted"),
                outcome("c", "conflict")]
    first = driver.assign_conflict_candidate_ids(outcomes)
    assert first == {"a": ["CAC_0001"], "c": ["CAC_0002"]}
    assert driver.assign_conflict_candidate_ids(outcomes) == first


def test_build_result_reuses_the_given_ids_rather_than_renumbering() -> None:
    """The other half of single ownership: no second numbering downstream."""
    config = copy.deepcopy(contracts.load_default_routing_config())
    item = driver.FieldExtractionOutcome(
        field_id="primary_diagnosis", domain_code="diagnosis", grade="A")
    item.status = "conflict"

    result = driver.build_result(
        case_id=CASE_ID, run_id=RUN_ID, outcomes=[item], config=config,
        documents=[], conflict_candidate_ids_by_field={
            "primary_diagnosis": ["CAC_0007"]})

    assert result["conflict_candidates"][0]["conflict_candidate_id"] == "CAC_0007"
    fact = next(row for row in result["claim_facts"]
                if row["field_id"] == "primary_diagnosis")
    assert fact["conflict_candidate_ids"] == ["CAC_0007"]


def test_a_conflict_with_no_assigned_id_is_refused_not_invented() -> None:
    """Failing loudly beats minting an id nothing else knows about."""
    config = copy.deepcopy(contracts.load_default_routing_config())
    item = driver.FieldExtractionOutcome(
        field_id="primary_diagnosis", domain_code="diagnosis", grade="A")
    item.status = "conflict"

    with pytest.raises(RuntimeError, match="conflict candidate id"):
        driver.build_result(
            case_id=CASE_ID, run_id=RUN_ID, outcomes=[item], config=config,
            documents=[], conflict_candidate_ids_by_field={})


def test_an_unsupported_link_still_makes_no_arbitrary_candidate(published) -> None:
    """Only a conflicting fact attaches an id; the others attach none."""
    result = _result(published)
    for link in result["policy_links"]:
        if link["coverage_id"] == "primary_diagnosis":
            continue
        for requirement in link["requirements"]:
            assert requirement.get("conflict_candidate_ids") == []
