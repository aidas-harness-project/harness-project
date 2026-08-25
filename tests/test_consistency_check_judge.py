"""`consistency_check` reaches its verdict through a provider call, not an agent.

`prepare` and `register` were always deterministic; the judgement between them
was a subagent, so it never touched `llm_providers` and could not be pointed at
a provider. `judge` fills that gap -- the hole Workstream B designed for and
nothing ever plugged into.

What is asserted here is mostly what the step REFUSES. Producing verdicts and
admitting them to the P6 ledger stay two commands on purpose, so these tests
check that `judge` cannot hand `register` anything `register` would have
refused, and cannot decide a disposition that is a human's.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import run_consistency_check as rcc  # noqa: E402
from llm_providers import FixtureProvider  # noqa: E402
from _validation import load_registry, validate_instance  # noqa: E402

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def work_items():
    return [
        {
            "conflict_candidate_id": "CAC_0001",
            "candidate_digest": DIGEST_A,
            "field_id": "diagnosis_side",
            "field_label": "진단 부위",
            "decision_bearing": True,
            "readings": [
                {"observation_id": "OBS_0001", "value_state": "asserted",
                 "value": "우측", "source_document_kind": "진단서",
                 "evidence_references": [
                     {"document_id": "DOC_001", "page": 2, "quote": "우측 요골 원위부 골절"}]},
                {"observation_id": "OBS_0002", "value_state": "asserted",
                 "value": "좌측", "source_document_kind": "수술기록지",
                 "evidence_references": [
                     {"document_id": "DOC_002", "page": 1, "quote": "좌측 요골"}]},
            ],
        },
        {
            "conflict_candidate_id": "CAC_0002",
            "candidate_digest": DIGEST_B,
            "field_id": "admission_days",
            "field_label": "입원일수",
            "decision_bearing": False,
            "readings": [
                {"observation_id": "OBS_0003", "value_state": "asserted",
                 "value": "14", "source_document_kind": "진단서",
                 "evidence_references": [
                     {"document_id": "DOC_001", "page": 3, "quote": "14일"}]},
                {"observation_id": "OBS_0004", "value_state": "asserted",
                 "value": "14일간", "source_document_kind": "진료비내역서",
                 "evidence_references": [
                     {"document_id": "DOC_003", "page": 1, "quote": "14일간 입원"}]},
            ],
        },
    ]


def response(*verdicts):
    return FixtureProvider(
        model_name="stub-judge",
        responses={"analyze_text_structured": json.dumps(
            {"verdicts": list(verdicts)}, ensure_ascii=False)})


CONFIRMED = {
    "conflict_candidate_id": "CAC_0001", "outcome": "confirmed",
    "reason": "One record says 우측, the other 좌측, for the same injury.",
    "professional_summary": "진단서는 우측, 수술기록지는 좌측으로 기재되어 있어 부위가 일치하지 않습니다. 어느 쪽이 맞는지 원본 확인이 필요합니다.",
}
WITHDRAWN = {
    "conflict_candidate_id": "CAC_0002", "outcome": "withdrawn",
    "reason": "'14' and '14일간' state the same duration in different form.",
}


# ------------------------------------------------------------------ prompt --

def test_the_prompt_carries_the_readings_and_asks_for_every_candidate():
    prompt = rcc.build_judge_prompt(work_items())

    for token in ("CAC_0001", "CAC_0002", "우측", "좌측",
                  "우측 요골 원위부 골절", "DOC_002", "decision_bearing: true"):
        assert token in prompt, f"missing from the prompt: {token!r}"


def test_the_prompt_suggests_no_verdict():
    # `prepare` withholds a verdict field because "a prepared answer would make
    # the decision by suggestion". The prompt must not reintroduce one.
    prompt = rcc.build_judge_prompt(work_items())
    body = prompt.split("### CAC_0001", 1)[1]
    assert "confirmed" not in body and "not_material" not in body


def test_the_prompt_opens_no_source_document():
    # Everything comes from the upstream contract, exactly as prepare does not
    # re-read the case. A prompt builder that reached for page chunks would be
    # introducing facts the work items do not contain.
    prompt = rcc.build_judge_prompt(work_items())
    assert "page_chunks" not in prompt and "read-redacted-text" not in prompt


# ------------------------------------------------------------- binding -----

def test_the_digest_is_copied_from_the_prepared_item():
    # The model is never asked for a digest and never believed about one: it is
    # a fact about what was prepared, not a judgement.
    bound = rcc.bind_verdicts({"verdicts": [CONFIRMED, WITHDRAWN]}, work_items())

    assert [v["candidate_digest"] for v in bound] == [DIGEST_A, DIGEST_B]


def test_a_digest_in_the_response_cannot_override_the_prepared_one():
    forged = {**CONFIRMED, "candidate_digest": "f" * 64}
    bound = rcc.bind_verdicts({"verdicts": [forged, WITHDRAWN]}, work_items())

    assert bound[0]["candidate_digest"] == DIGEST_A


def test_the_transport_schema_does_not_even_offer_a_digest_field():
    verdict = rcc.JUDGE_OUTPUT_SCHEMA["properties"]["verdicts"]["items"]
    assert "candidate_digest" not in verdict["properties"]
    assert verdict["additionalProperties"] is False


@pytest.mark.parametrize("raw,expected", [
    ({"verdicts": [CONFIRMED]}, "no verdict for: CAC_0002"),
    ({"verdicts": [CONFIRMED, WITHDRAWN,
                   {**WITHDRAWN, "conflict_candidate_id": "CAC_0009"}]},
     "no such prepared candidate"),
    ({"verdicts": [CONFIRMED, CONFIRMED, WITHDRAWN]}, "judged more than once"),
    ({"verdicts": []}, "no verdict for"),
])
def test_a_response_register_would_refuse_is_refused_here_first(raw, expected):
    with pytest.raises(ValueError) as excinfo:
        rcc.bind_verdicts(raw, work_items())
    assert expected in str(excinfo.value)


def test_a_confirmed_verdict_without_a_summary_is_refused():
    bare = {k: v for k, v in CONFIRMED.items() if k != "professional_summary"}
    with pytest.raises(ValueError):
        rcc.bind_verdicts({"verdicts": [bare, WITHDRAWN]}, work_items())


# --------------------------------------------------------------- the step --

def test_judge_writes_a_contract_that_validates(monkeypatch, tmp_path):
    written = {}
    monkeypatch.setattr(rcc, "require_open_attempt", lambda *a, **k: None)
    monkeypatch.setattr(rcc, "_dao_json", lambda *a, **k: {
        "claim_analysis_sha256": "c" * 64, "work_items": work_items()})

    def fake_write(args):
        written["contract"] = json.loads(
            Path(args[args.index("--data-file") + 1]).read_text(encoding="utf-8"))
        written["name"] = args[2]
        return ""

    monkeypatch.setattr(rcc, "_dao_write", fake_write)

    result = rcc.run_judge(case_id="CASE_999", run_id="RUN_20260826_001", held_by="tester",
                           provider=response(CONFIRMED, WITHDRAWN))

    assert result == {"judged": 2, "outcomes": {"confirmed": 1, "withdrawn": 1}}
    assert written["name"] == "consistency_check_verdicts.json"
    contract = written["contract"]
    assert contract["model_info"]["model_name"] == "fixture:stub-judge"
    assert contract["review_required"] is True
    schemas, registry = load_registry()
    assert validate_instance(contract, "consistency_check_verdicts.schema.json",
                             schemas, registry) == []


def test_judge_registers_nothing(monkeypatch):
    # Producing verdicts and admitting them to the P6 ledger stay two commands,
    # so the deterministic admission checks still run against whatever wrote
    # them -- an agent or this step.
    monkeypatch.setattr(rcc, "require_open_attempt", lambda *a, **k: None)
    monkeypatch.setattr(rcc, "_dao_json", lambda *a, **k: {
        "claim_analysis_sha256": "c" * 64, "work_items": work_items()})
    monkeypatch.setattr(rcc, "_dao_write", lambda args: "")
    called = []
    monkeypatch.setattr(rcc, "register_confirmed",
                        lambda *a, **k: called.append(a) or {})

    rcc.run_judge(case_id="CASE_999", run_id="RUN_20260826_001", held_by="tester",
                  provider=response(CONFIRMED, WITHDRAWN))

    assert called == []


def test_nothing_prepared_means_nothing_judged(monkeypatch):
    # An empty verdicts contract would be a judgement about no candidates.
    monkeypatch.setattr(rcc, "require_open_attempt", lambda *a, **k: None)
    monkeypatch.setattr(rcc, "_dao_json", lambda *a, **k: {
        "claim_analysis_sha256": "c" * 64, "work_items": []})
    wrote = []
    monkeypatch.setattr(rcc, "_dao_write", lambda args: wrote.append(args) or "")

    class Exploding:
        provider_name = "none"
        model_name = "none"

        def analyze_text_structured(self, *a, **k):
            raise AssertionError("no provider call may be made")

    result = rcc.run_judge(case_id="CASE_999", run_id="RUN_20260826_001", held_by="tester",
                           provider=Exploding())

    assert result["judged"] == 0
    assert wrote == []


def test_a_bad_response_gets_exactly_one_correction(monkeypatch):
    # P4: one correction, then the caller's failure. Not a retry loop.
    monkeypatch.setattr(rcc, "require_open_attempt", lambda *a, **k: None)
    monkeypatch.setattr(rcc, "_dao_json", lambda *a, **k: {
        "claim_analysis_sha256": "c" * 64, "work_items": work_items()})
    monkeypatch.setattr(rcc, "_dao_write", lambda args: "")
    calls = []

    class Stubborn:
        provider_name = "fixture"
        model_name = "stub"

        def analyze_text_structured(self, prompt, prompt_version, output_schema):
            calls.append(prompt)
            from llm_providers import ProviderResult
            payload = {"verdicts": [CONFIRMED]}   # always misses CAC_0002
            return ProviderResult(
                provider_name="fixture", model_name="stub",
                prompt_version=prompt_version,
                text=json.dumps(payload), structured_output=payload)

    with pytest.raises(Exception):
        rcc.run_judge(case_id="CASE_999", run_id="RUN_20260826_001", held_by="tester",
                      provider=Stubborn())

    assert len(calls) == 2, "exactly one correction attempt, no more"


def test_the_cli_exposes_judge_between_prepare_and_register():
    import argparse
    parser = argparse.ArgumentParser()
    # Cheapest faithful check: the real parser's choices, read from --help.
    import subprocess
    out = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "run_consistency_check.py"), "--help"],
        capture_output=True, text=True, timeout=60).stdout
    assert "{prepare,judge,register}" in out
    assert "--provider" in out and "--model" in out
