"""The critic's floors run outside the model turn; the two judgements stay in it.

Four of the six fields in `critic_result` were already mechanical, but nothing
ran those checks outside an agent's own tool calls -- so the counts and the
reading came from the same pass, and a run that skipped a check looked like one
that passed it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import run_critic  # noqa: E402
from _validation import load_registry, validate_instance  # noqa: E402

CHECKS_CLEAN = {
    "evidence_tags": {"consistent": True, "orphaned_tags": [], "unused_citations": []},
    "forbidden": {"ran": True, "clean": True, "hits": []},
    "untagged": {"ran": True, "clean": True, "findings": []},
}
CHECKS_DIRTY = {
    "evidence_tags": {"orphaned_tags": ["E7"], "unused_citations": ["E3", "E4"]},
    "forbidden": {"ran": True, "hits": [{"line": 12, "phrase": "확실히 보장됩니다"}]},
    "untagged": {"ran": True, "findings": ["IV. 판단 -- 후유장해가 인정된다"]},
}
CHECKS_SKIPPED = {
    "evidence_tags": {"orphaned_tags": [], "unused_citations": []},
    "forbidden": {"ran": False, "reason": "NO_TEMPLATE: templates/forbidden-expressions.md"},
    "untagged": {"ran": False, "reason": "NO_MATCHING_SECTIONS: none appear"},
}
GOOD = {"passed": False, "findings": [
    {"finding_type": "unhedged_inference", "description": "추정을 사실처럼 기술했습니다.",
     "severity": "high", "suggested_fix": "'…로 추정된다'로 완화", "heading": "IV. 판단",
     "tag": "E7"}]}


# --------------------------------------------------------------- the gate --

def test_passed_is_taken_as_given_never_computed():
    # The schema is explicit: "not inferred from findings.length -- lets critic
    # mark passed:false on a severity judgment call even alongside minor-only
    # findings". Computing it would delete the judgement the field carries.
    assert run_critic.bind_result({"passed": True, "findings": []})["passed"] is True
    minor = {"passed": False, "findings": [
        {"finding_type": "forbidden_expression", "description": "x", "severity": "low"}]}
    assert run_critic.bind_result(minor)["passed"] is False
    clean_but_failed = {"passed": False, "findings": []}
    assert run_critic.bind_result(clean_but_failed)["passed"] is False


def test_a_missing_passed_is_refused_rather_than_defaulted():
    with pytest.raises(ValueError):
        run_critic.bind_result({"findings": []})
    with pytest.raises(ValueError):
        run_critic.bind_result({"passed": "yes", "findings": []})


def test_the_model_cannot_supply_a_finding_id():
    schema_props = run_critic.OUTPUT_SCHEMA["properties"]["findings"]["items"]
    assert "finding_id" not in schema_props["properties"]
    assert schema_props["additionalProperties"] is False
    bound = run_critic.bind_result(GOOD)
    assert bound["findings"][0]["finding_id"] == "CF-1"


def test_location_is_assembled_from_the_flat_fields():
    bound = run_critic.bind_result(GOOD)
    assert bound["findings"][0]["location"] == {"heading": "IV. 판단", "tag": "E7"}


# ------------------------------------------------------ a skipped check --

def test_a_check_that_did_not_run_records_no_count():
    # A count of zero says "the check ran and found nothing" -- exactly the
    # claim a failed check must not make. The schema leaves both optional for
    # this reason.
    contract = run_critic.build_contract(
        case_id="CASE_999", run_id="RUN_20260826_001",
        reviewed_document="outputs/CASE_999/draft_report_v1.md",
        checks=CHECKS_SKIPPED, result=run_critic.bind_result(GOOD),
        model_name="fixture:stub")

    assert "forbidden_literal_hit_count" not in contract
    assert "untagged_claim_candidate_count" not in contract
    assert len(contract["warnings"]) == 2
    assert any("did not run" in w for w in contract["warnings"])


def test_a_check_that_ran_clean_records_a_real_zero():
    contract = run_critic.build_contract(
        case_id="CASE_999", run_id="RUN_20260826_001",
        reviewed_document="outputs/CASE_999/draft_report_v1.md",
        checks=CHECKS_CLEAN, result=run_critic.bind_result(GOOD),
        model_name="fixture:stub")

    assert contract["forbidden_literal_hit_count"] == 0
    assert contract["untagged_claim_candidate_count"] == 0
    assert contract["warnings"] == []


def test_the_counts_come_from_the_checks_not_the_model():
    contract = run_critic.build_contract(
        case_id="CASE_999", run_id="RUN_20260826_001",
        reviewed_document="outputs/CASE_999/draft_report_v1.md",
        checks=CHECKS_DIRTY, result=run_critic.bind_result(GOOD),
        model_name="fixture:stub")

    assert contract["orphaned_tag_count"] == 1
    assert contract["unused_citation_count"] == 2
    assert contract["forbidden_literal_hit_count"] == 1
    assert contract["untagged_claim_candidate_count"] == 1


def test_the_contract_validates():
    contract = run_critic.build_contract(
        case_id="CASE_999", run_id="RUN_20260826_001",
        reviewed_document="outputs/CASE_999/draft_report_v1.md",
        checks=CHECKS_DIRTY, result=run_critic.bind_result(GOOD),
        model_name="fixture:stub")
    schemas, registry = load_registry()
    assert validate_instance(contract, "critic_result.schema.json",
                             schemas, registry) == []


# --------------------------------------------------------------- blind --

def test_the_prompt_carries_the_draft_the_sidecar_and_the_check_output():
    prompt = run_critic.build_prompt(
        draft="## IV. 판단\n후유장해가 인정된다 [E7]",
        sidecar={"citations": [{"tag": "E7", "document_id": "DOC_001",
                                "page": 3, "quote": "장해율 20%"}]},
        checks=CHECKS_DIRTY)

    for token in ("후유장해가 인정된다", "E7", "DOC_001", "장해율 20%",
                  "확실히 보장됩니다", "orphaned"):
        assert token in prompt, f"missing from the prompt: {token!r}"


def test_the_prompt_reaches_no_ground_truth():
    # D1 held structurally: the prompt is the draft, its sidecar and the check
    # output, so blindness is a property of what was assembled rather than an
    # instruction the model is asked to obey.
    prompt = run_critic.build_prompt(
        draft="draft body", sidecar={"citations": []}, checks=CHECKS_CLEAN)
    for forbidden in ("source-cases", "ground_truth", "data/ground_truth"):
        assert forbidden not in prompt


def test_the_prompt_says_a_candidate_is_not_a_finding():
    # A flagged line may legitimately owe no citation; a non-zero count with no
    # corresponding finding is a valid outcome.
    prompt = run_critic.build_prompt(
        draft="d", sidecar={"citations": []}, checks=CHECKS_DIRTY)
    # Whitespace-normalised: the instruction text is hard-wrapped, so a raw
    # substring search would fail on the line break rather than on the content.
    flattened = " ".join(prompt.split())
    assert "CANDIDATES (not findings)" in flattened
    assert "a candidate is not a finding" in flattened
    assert "owes no citation of its own" in flattened


def test_a_skipped_check_is_named_in_the_prompt_not_shown_as_clean():
    prompt = run_critic.build_prompt(
        draft="d", sidecar={"citations": []}, checks=CHECKS_SKIPPED)
    assert "DID NOT RUN" in prompt


# ---------------------------------------------------------------- wiring --

def test_a_missing_draft_blocks_before_any_provider_call(tmp_path):
    class Exploding:
        provider_name = "none"
        model_name = "none"

        def analyze_text_structured(self, *a, **k):
            raise AssertionError("no provider call may be made")

    with pytest.raises(RuntimeError) as excinfo:
        run_critic.run(case_id="CASE_NO_SUCH", version="v1",
                       template="배상책임_후유장해형", run_id="RUN_20260826_001",
                       held_by="tester", provider=Exploding())

    assert "no draft to review" in str(excinfo.value)


def test_the_cli_requires_the_template_that_scopes_the_scan():
    import subprocess
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "run_critic.py"), "CASE_999", "v1",
         "--held-by", "t", "--run-id", "RUN_20260826_001"],
        capture_output=True, text=True, timeout=60)
    # Passing the wrong template matches no section and is refused rather than
    # reported clean, so the flag cannot be optional.
    assert proc.returncode != 0
    assert "--template" in proc.stderr
