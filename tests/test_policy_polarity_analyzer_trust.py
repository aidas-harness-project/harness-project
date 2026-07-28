"""P1-2 follow-up: the semantic analyzer's trust boundary.

Four approval-blocking defects the original P1-2 receipt shipped with, each
reproduced here as an attack:

  A. the expected bucket was written into the classification prompt, so the
     LLM was told the answer Python would accept before it read the source;
  B. the caller chose provider/model, so one ordinary analysis call could
     re-point `active_analyzer` and silently strand every issued receipt;
  C. no semantic state reached `policy_document_digest`, so an analyzer swap
     left every downstream `upstream_policy_snapshot` looking current;
  D. `--condition-text-file` read an arbitrary path, so any file in (or out
     of) the repo could be shipped to a provider as "condition text".

The regression cases in section 6 of the task assert only what a unit test can
honestly assert -- prompt content, phase separation, parser strictness, and the
Python-side comparison. They are NOT a claim about real model accuracy.
"""
import json

import _cross_contract
import dao
import llm_providers
import policy_polarity_semantics as semantics

from test_policy_polarity_semantic_receipt import (
    CASE, DOC, RUN, SOURCE, CountingProvider, analysis_json, args_for,
    normalized_contract, seed_case,
)


# --------------------------------------------------------------------------
# A. the source classification prompt must not carry the expected bucket
# --------------------------------------------------------------------------

def test_source_classification_prompt_never_names_a_bucket():
    """Attack A: the LLM must classify the source without being shown the
    answer Python will accept."""
    prompt = semantics.build_source_prompt(SOURCE)
    assert "BUCKET" not in prompt
    for bucket in (
        "payout_conditions", "exclusions", "reduction_conditions",
        "coverage_start_conditions",
    ):
        assert bucket not in prompt


def test_source_classification_prompt_never_carries_condition_text():
    """Attack A (second half): a condition the caller extracted is itself a
    hint about the intended reading, so Phase A must not see it either."""
    condition = "지급 대상 조건 텍스트"
    prompt = semantics.build_source_prompt(SOURCE)
    assert condition not in prompt
    assert "CONDITION_TEXT" not in prompt


def test_comparison_prompt_never_names_a_bucket():
    """Phase B compares meaning only; the bucket expectation stays in Python."""
    prompt = semantics.build_comparison_prompt(SOURCE, "조건 텍스트")
    assert "BUCKET" not in prompt
    for bucket in (
        "payout_conditions", "exclusions", "reduction_conditions",
        "coverage_start_conditions",
    ):
        assert bucket not in prompt
    assert "조건 텍스트" in prompt


def test_issued_receipt_prompts_are_bucket_free_end_to_end(
        isolated_dao, make_args, canonicalize, monkeypatch):
    """The real command's provider sees no bucket in any prompt it is sent."""
    span_uid, selector, condition = seed_case(
        isolated_dao, make_args, canonicalize)
    provider = TwoPhaseProvider()
    monkeypatch.setattr(
        dao.llm_providers, "build_provider", lambda *a, **k: provider)
    monkeypatch.setattr(dao, "resolve_trusted_analyzer", _trusted(provider))

    assert dao.cmd_analyze_policy_polarity(
        args_for(make_args, span_uid, selector, condition)) == 0
    assert provider.calls == 2
    source_prompt = provider.prompts[0]
    assert "BUCKET" not in source_prompt
    assert "payout_conditions" not in source_prompt
    assert SOURCE in source_prompt
    for prompt in provider.prompts:
        assert "payout_conditions" not in prompt


# --------------------------------------------------------------------------
# B. the caller must not be able to choose the analyzer
# --------------------------------------------------------------------------

def test_cli_rejects_caller_supplied_provider_and_model():
    """Attack B: `--provider`/`--model` are gone from the production command."""
    parser = dao.build_parser()
    for flag in ("--provider", "--model"):
        with __import__("pytest").raises(SystemExit):
            parser.parse_args([
                "analyze-policy-polarity", CASE, "--doc-id", DOC,
                "--source-span-uid", "PS-" + "1" * 16,
                "--source-selector-json", "{}",
                "--condition-text", "x",
                "--bucket", "payout_conditions",
                flag, "fixture",
                "--held-by", "a", "--run-id", RUN,
            ])


def test_production_cli_cannot_select_the_fixture_provider(monkeypatch):
    """Attack B: even the deployment config refuses `fixture` in production."""
    monkeypatch.setenv("HARNESS_POLICY_SEMANTIC_PROVIDER", "fixture")
    monkeypatch.setenv("HARNESS_POLICY_SEMANTIC_MODEL", "anything")
    try:
        dao.resolve_trusted_analyzer()
    except dao.llm_providers.ProviderConfigError as exc:
        assert "fixture" in str(exc)
    else:
        raise AssertionError("fixture must not be selectable in production")


def test_analysis_call_cannot_repoint_the_active_analyzer(
        isolated_dao, make_args, canonicalize, monkeypatch):
    """Attack B: an ordinary analysis call under a different analyzer is
    refused outright -- it may not silently rewrite `active_analyzer` and
    strand every receipt already issued."""
    span_uid, selector, condition = seed_case(
        isolated_dao, make_args, canonicalize)
    first = TwoPhaseProvider()
    monkeypatch.setattr(
        dao.llm_providers, "build_provider", lambda *a, **k: first)
    monkeypatch.setattr(dao, "resolve_trusted_analyzer", _trusted(first))
    assert dao.cmd_analyze_policy_polarity(
        args_for(make_args, span_uid, selector, condition)) == 0
    before = dao.load_policy_polarity_semantic_index(CASE)

    second = TwoPhaseProvider(model_name="semantic-fixture-v2")
    monkeypatch.setattr(
        dao.llm_providers, "build_provider", lambda *a, **k: second)
    monkeypatch.setattr(dao, "resolve_trusted_analyzer", _trusted(second))
    condition2 = isolated_dao / "condition_other.txt"
    condition2.write_text(SOURCE + " 추가", encoding="utf-8")
    rc = dao.cmd_analyze_policy_polarity(
        args_for(make_args, span_uid, selector, condition2))

    assert rc == 1
    assert second.calls == 0
    assert dao.load_policy_polarity_semantic_index(CASE) == before


def test_analyzer_change_fails_policy_stage_and_all_downstream(
        isolated_dao, make_args, canonicalize, monkeypatch):
    """Attack B/9: a deliberate analyzer switch is a DAO admin operation and
    must cascade fail-closed through the policy stage and everything below."""
    span_uid, selector, condition = seed_case(
        isolated_dao, make_args, canonicalize)
    provider = TwoPhaseProvider()
    monkeypatch.setattr(
        dao.llm_providers, "build_provider", lambda *a, **k: provider)
    monkeypatch.setattr(dao, "resolve_trusted_analyzer", _trusted(provider))
    assert dao.cmd_analyze_policy_polarity(
        args_for(make_args, span_uid, selector, condition)) == 0
    _seed_passed_stages(make_args)

    replacement = TwoPhaseProvider(model_name="semantic-fixture-v2")
    monkeypatch.setattr(
        dao.llm_providers, "build_provider", lambda *a, **k: replacement)
    monkeypatch.setattr(
        dao, "resolve_trusted_analyzer", _trusted(replacement))
    assert dao.cmd_set_policy_semantic_analyzer(make_args(
        case_id=CASE, reason="analyzer upgrade",
        held_by="dao-admin", run_id=RUN)) == 0

    statuses = {
        entry["stage_name"]: entry["status"]
        for entry in dao.load_run_state(CASE)["stages"]}
    assert statuses["policy_clause_processing"] == "failed"
    for stage in dao.stage_dependencies.dependents_of(
            "policy_clause_processing"):
        if stage in statuses:
            assert statuses[stage] == "failed", stage


def test_analyzer_change_leaves_downstream_snapshot_mismatched(
        isolated_dao, make_args, canonicalize, monkeypatch):
    """Attack C/10: after the switch, a snapshot taken under the old analyzer
    no longer matches the live policy digest."""
    span_uid, selector, condition = seed_case(
        isolated_dao, make_args, canonicalize)
    provider = TwoPhaseProvider()
    monkeypatch.setattr(
        dao.llm_providers, "build_provider", lambda *a, **k: provider)
    monkeypatch.setattr(dao, "resolve_trusted_analyzer", _trusted(provider))
    assert dao.cmd_analyze_policy_polarity(
        args_for(make_args, span_uid, selector, condition)) == 0
    _write_normalized(isolated_dao, selector)
    before = dao.policy_snapshot_for(CASE, [DOC])

    replacement = TwoPhaseProvider(model_name="semantic-fixture-v2")
    monkeypatch.setattr(
        dao.llm_providers, "build_provider", lambda *a, **k: replacement)
    monkeypatch.setattr(
        dao, "resolve_trusted_analyzer", _trusted(replacement))
    assert dao.cmd_set_policy_semantic_analyzer(make_args(
        case_id=CASE, reason="analyzer upgrade",
        held_by="dao-admin", run_id=RUN)) == 0

    after = dao.policy_snapshot_for(CASE, [DOC])
    assert after["snapshot_sha256"] != before["snapshot_sha256"]


def test_failed_cascade_never_activates_the_new_analyzer(
        isolated_dao, make_args, canonicalize, monkeypatch):
    """Attack 16: fault-inject the cascade; the forbidden end state is a new
    analyzer sitting on top of a still-passed policy stage."""
    span_uid, selector, condition = seed_case(
        isolated_dao, make_args, canonicalize)
    provider = TwoPhaseProvider()
    monkeypatch.setattr(
        dao.llm_providers, "build_provider", lambda *a, **k: provider)
    monkeypatch.setattr(dao, "resolve_trusted_analyzer", _trusted(provider))
    assert dao.cmd_analyze_policy_polarity(
        args_for(make_args, span_uid, selector, condition)) == 0
    _seed_passed_stages(make_args)
    original = dao.load_policy_polarity_semantic_index(CASE)["active_analyzer"]

    replacement = TwoPhaseProvider(model_name="semantic-fixture-v2")
    monkeypatch.setattr(
        dao.llm_providers, "build_provider", lambda *a, **k: replacement)
    monkeypatch.setattr(
        dao, "resolve_trusted_analyzer", _trusted(replacement))

    def boom(*args, **kwargs):
        raise dao.CascadeFailed("fault injection: run-state lock held")

    monkeypatch.setattr(dao, "_invalidate_policy_layer", boom)
    assert dao.cmd_set_policy_semantic_analyzer(make_args(
        case_id=CASE, reason="analyzer upgrade",
        held_by="dao-admin", run_id=RUN)) == 1

    index = dao.load_policy_polarity_semantic_index(CASE)
    assert index["active_analyzer"] == original
    assert dao._semantic_index_errors(index, CASE) == []


# --------------------------------------------------------------------------
# C. semantic binding must reach the per-document policy snapshot
# --------------------------------------------------------------------------

def test_referenced_receipt_change_moves_the_document_digest(
        isolated_dao, make_args, canonicalize, monkeypatch):
    """Attack C: the digest must move when the receipt the contract cites
    changes."""
    span_uid, selector, condition = seed_case(
        isolated_dao, make_args, canonicalize)
    provider = TwoPhaseProvider()
    monkeypatch.setattr(
        dao.llm_providers, "build_provider", lambda *a, **k: provider)
    monkeypatch.setattr(dao, "resolve_trusted_analyzer", _trusted(provider))
    assert dao.cmd_analyze_policy_polarity(
        args_for(make_args, span_uid, selector, condition)) == 0
    receipt = dao.load_policy_polarity_semantic_index(CASE)["receipts"][0]
    _write_normalized(isolated_dao, selector, receipt["receipt_id"])
    with_receipt = dao.policy_document_digest(CASE, DOC)

    _write_normalized(isolated_dao, selector, receipt_id=None)
    assert dao.policy_document_digest(CASE, DOC) != with_receipt


def test_unrelated_document_receipt_does_not_move_this_digest(
        isolated_dao, make_args, canonicalize, monkeypatch):
    """Attack C/11: per-document hashing, not a whole-index hash -- another
    document's receipt must not stale this one."""
    span_uid, selector, condition = seed_case(
        isolated_dao, make_args, canonicalize)
    provider = TwoPhaseProvider()
    monkeypatch.setattr(
        dao.llm_providers, "build_provider", lambda *a, **k: provider)
    monkeypatch.setattr(dao, "resolve_trusted_analyzer", _trusted(provider))
    assert dao.cmd_analyze_policy_polarity(
        args_for(make_args, span_uid, selector, condition)) == 0
    receipt = dao.load_policy_polarity_semantic_index(CASE)["receipts"][0]
    _write_normalized(isolated_dao, selector, receipt["receipt_id"])
    before = dao.policy_document_digest(CASE, DOC)

    index = dao.load_policy_polarity_semantic_index(CASE)
    stranger = json.loads(json.dumps(receipt))
    stranger["document_id"] = "DOC_099"
    stranger["receipt_id"] = semantics.receipt_id(stranger)
    index["receipts"].append(stranger)
    index["index_id"] = semantics.index_id(index)
    dao.atomic_write_json(
        dao.policy_polarity_semantic_index_path(CASE), index)

    assert dao.policy_document_digest(CASE, DOC) == before


def test_repeat_analysis_cache_hit_is_a_noop(
        isolated_dao, make_args, canonicalize, monkeypatch):
    """Attack 12: same analyzer + same inputs calls no provider and moves no
    digest."""
    span_uid, selector, condition = seed_case(
        isolated_dao, make_args, canonicalize)
    provider = TwoPhaseProvider()
    monkeypatch.setattr(
        dao.llm_providers, "build_provider", lambda *a, **k: provider)
    monkeypatch.setattr(dao, "resolve_trusted_analyzer", _trusted(provider))
    args = args_for(make_args, span_uid, selector, condition)
    assert dao.cmd_analyze_policy_polarity(args) == 0
    receipt = dao.load_policy_polarity_semantic_index(CASE)["receipts"][0]
    _write_normalized(isolated_dao, selector, receipt["receipt_id"])
    before = dao.policy_document_digest(CASE, DOC)
    calls = provider.calls

    assert dao.cmd_analyze_policy_polarity(args) == 0
    assert provider.calls == calls
    assert len(dao.load_policy_polarity_semantic_index(CASE)["receipts"]) == 1
    assert dao.policy_document_digest(CASE, DOC) == before


# --------------------------------------------------------------------------
# D. arbitrary file paths must never reach a provider
# --------------------------------------------------------------------------

def test_protected_path_as_condition_input_never_reaches_a_provider(
        isolated_dao, make_args, canonicalize, monkeypatch, tmp_path):
    """Attack D/13: the file-path input is gone. A caller pointing at a
    protected tree gets a parse-time refusal, with zero provider calls."""
    span_uid, selector, condition = seed_case(
        isolated_dao, make_args, canonicalize)
    provider = TwoPhaseProvider()
    monkeypatch.setattr(
        dao.llm_providers, "build_provider", lambda *a, **k: provider)
    monkeypatch.setattr(dao, "resolve_trusted_analyzer", _trusted(provider))

    parser = dao.build_parser()
    for protected in (
        "source-cases/secret.md",
        "archive/sources/secret.md",
        "outputs/CASE_030/normalized_policy_clause_DOC_001.json",
        "data/ground_truth/CASE_030/GT_001.txt",
        str(tmp_path / "outside.txt"),
    ):
        with __import__("pytest").raises(SystemExit):
            parser.parse_args([
                "analyze-policy-polarity", CASE, "--doc-id", DOC,
                "--source-span-uid", span_uid,
                "--source-selector-json", "{}",
                "--condition-text-file", protected,
                "--bucket", "payout_conditions",
                "--held-by", "a", "--run-id", RUN,
            ])
    assert provider.calls == 0


def test_source_selector_is_inline_json_not_a_file_path(
        isolated_dao, make_args, canonicalize, monkeypatch):
    """Attack D: the selector is inline JSON too -- no second file-read path
    back into the provider."""
    span_uid, selector, condition = seed_case(
        isolated_dao, make_args, canonicalize)
    provider = TwoPhaseProvider()
    monkeypatch.setattr(
        dao.llm_providers, "build_provider", lambda *a, **k: provider)
    monkeypatch.setattr(dao, "resolve_trusted_analyzer", _trusted(provider))
    parser = dao.build_parser()
    with __import__("pytest").raises(SystemExit):
        parser.parse_args([
            "analyze-policy-polarity", CASE, "--doc-id", DOC,
            "--source-span-uid", span_uid,
            "--source-selector-file", "source-cases/secret.json",
            "--condition-text", SOURCE,
            "--bucket", "payout_conditions",
            "--held-by", "a", "--run-id", RUN,
        ])
    assert provider.calls == 0


# --------------------------------------------------------------------------
# Python owns the bucket comparison; the receipt only reports the source
# --------------------------------------------------------------------------

def test_negative_source_under_payout_bucket_is_refused(
        isolated_dao, make_args, canonicalize, monkeypatch):
    """Attack 3: the receipt stays `restrictive_or_negative` even though the
    caller submitted `payout_conditions`, and Python refuses the pairing."""
    span_uid, selector, condition = seed_case(
        isolated_dao, make_args, canonicalize)
    provider = TwoPhaseProvider(
        source=analysis_json(classification="restrictive_or_negative"))
    monkeypatch.setattr(
        dao.llm_providers, "build_provider", lambda *a, **k: provider)
    monkeypatch.setattr(dao, "resolve_trusted_analyzer", _trusted(provider))
    assert dao.cmd_analyze_policy_polarity(
        args_for(make_args, span_uid, selector, condition)) == 0

    receipt = dao.load_policy_polarity_semantic_index(CASE)["receipts"][0]
    assert receipt["source_receipt"]["source_classification"] == \
        "restrictive_or_negative"
    span = json.loads(selector.read_text(encoding="utf-8"))["source_spans"][0]
    contract = normalized_contract(span, receipt["receipt_id"])
    errors = dao._semantic_receipt_binding_errors(
        CASE, DOC, contract["clauses"][0]["payout_conditions"][0],
        "payout_conditions", "condition")
    assert any("requires affirmative" in error for error in errors)


def test_affirmative_source_under_exclusions_bucket_is_refused(
        isolated_dao, make_args, canonicalize, monkeypatch):
    """Attack 4: the mirror case."""
    span_uid, selector, condition = seed_case(
        isolated_dao, make_args, canonicalize)
    provider = TwoPhaseProvider(source=analysis_json())
    monkeypatch.setattr(
        dao.llm_providers, "build_provider", lambda *a, **k: provider)
    monkeypatch.setattr(dao, "resolve_trusted_analyzer", _trusted(provider))
    assert dao.cmd_analyze_policy_polarity(args_for(
        make_args, span_uid, selector, condition,
        bucket="exclusions")) == 0

    receipt = dao.load_policy_polarity_semantic_index(CASE)["receipts"][0]
    span = json.loads(selector.read_text(encoding="utf-8"))["source_spans"][0]
    contract = normalized_contract(
        span, receipt["receipt_id"], bucket="exclusions")
    errors = dao._semantic_receipt_binding_errors(
        CASE, DOC, contract["clauses"][0]["exclusions"][0],
        "exclusions", "condition")
    assert any("requires restrictive_or_negative" in error
               for error in errors)


def test_meaning_not_preserved_is_refused(
        isolated_dao, make_args, canonicalize, monkeypatch):
    """Attack 6: Phase B saying the condition drifted blocks the condition."""
    span_uid, selector, condition = seed_case(
        isolated_dao, make_args, canonicalize)
    provider = TwoPhaseProvider(
        comparison=comparison_json(
            meaning_preserved=False, review_required=True,
            omitted=["지급 제한 명제"]))
    monkeypatch.setattr(
        dao.llm_providers, "build_provider", lambda *a, **k: provider)
    monkeypatch.setattr(dao, "resolve_trusted_analyzer", _trusted(provider))
    assert dao.cmd_analyze_policy_polarity(
        args_for(make_args, span_uid, selector, condition)) == 0

    receipt = dao.load_policy_polarity_semantic_index(CASE)["receipts"][0]
    span = json.loads(selector.read_text(encoding="utf-8"))["source_spans"][0]
    contract = normalized_contract(span, receipt["receipt_id"])
    errors = dao._semantic_receipt_binding_errors(
        CASE, DOC, contract["clauses"][0]["payout_conditions"][0],
        "payout_conditions", "condition")
    assert any("does not preserve the source meaning" in error
               for error in errors)


def test_mixed_and_ambiguous_sources_are_always_refused(
        isolated_dao, make_args, canonicalize, monkeypatch):
    """Attack 5: neither bucket accepts an unsettled source reading."""
    for classification in ("mixed", "ambiguous"):
        span_uid, selector, condition = seed_case(
            isolated_dao, make_args, canonicalize)
        dao.policy_polarity_semantic_index_path(CASE).unlink(missing_ok=True)
        payload = json.loads(analysis_json(
            classification=classification, review_required=True))
        payload["propositions"][0]["classification"] = (
            "affirmative" if classification == "mixed" else "ambiguous")
        provider = TwoPhaseProvider(
            source=json.dumps(payload, ensure_ascii=False))
        monkeypatch.setattr(
            dao.llm_providers, "build_provider", lambda *a, **k: provider)
        monkeypatch.setattr(
            dao, "resolve_trusted_analyzer", _trusted(provider))
        assert dao.cmd_analyze_policy_polarity(
            args_for(make_args, span_uid, selector, condition)) == 0
        receipt = dao.load_policy_polarity_semantic_index(
            CASE)["receipts"][0]
        span = json.loads(
            selector.read_text(encoding="utf-8"))["source_spans"][0]
        contract = normalized_contract(span, receipt["receipt_id"])
        errors = dao._semantic_receipt_binding_errors(
            CASE, DOC, contract["clauses"][0]["payout_conditions"][0],
            "payout_conditions", "condition")
        assert any(f"is {classification}" in error for error in errors)


def test_phase_b_schema_mismatch_issues_no_receipt(
        isolated_dao, make_args, canonicalize, monkeypatch):
    """Attack 14: a malformed comparison response leaves nothing on disk --
    Phase A having succeeded is not a partial pass."""
    span_uid, selector, condition = seed_case(
        isolated_dao, make_args, canonicalize)
    provider = TwoPhaseProvider(comparison='{"meaning_preserved": true}')
    monkeypatch.setattr(
        dao.llm_providers, "build_provider", lambda *a, **k: provider)
    monkeypatch.setattr(dao, "resolve_trusted_analyzer", _trusted(provider))
    assert dao.cmd_analyze_policy_polarity(
        args_for(make_args, span_uid, selector, condition)) == 1
    assert not dao.policy_polarity_semantic_index_path(CASE).exists()


def test_validation_and_finalization_never_build_a_provider(
        isolated_dao, make_args, canonicalize, monkeypatch):
    """Attack 15: neither the binding validator nor the finalization gate may
    reach for a provider."""
    span_uid, selector, condition = seed_case(
        isolated_dao, make_args, canonicalize)
    provider = TwoPhaseProvider()
    monkeypatch.setattr(
        dao.llm_providers, "build_provider", lambda *a, **k: provider)
    monkeypatch.setattr(dao, "resolve_trusted_analyzer", _trusted(provider))
    assert dao.cmd_analyze_policy_polarity(
        args_for(make_args, span_uid, selector, condition)) == 0
    receipt = dao.load_policy_polarity_semantic_index(CASE)["receipts"][0]
    _write_normalized(isolated_dao, selector, receipt["receipt_id"])

    def explode(*args, **kwargs):
        raise AssertionError("no provider may be built off the analysis path")

    monkeypatch.setattr(dao.llm_providers, "build_provider", explode)
    span = json.loads(selector.read_text(encoding="utf-8"))["source_spans"][0]
    contract = normalized_contract(span, receipt["receipt_id"])
    assert dao._semantic_receipt_binding_errors(
        CASE, DOC, contract["clauses"][0]["payout_conditions"][0],
        "payout_conditions", "condition") == []
    dao._policy_completion_blockers(CASE)
    dao.policy_document_digest(CASE, DOC)


# --------------------------------------------------------------------------
# Section 6 regression corpus -- parser/prompt properties only, NOT accuracy
# --------------------------------------------------------------------------

def test_source_only_regression_corpus_is_parsed_without_bucket_hints():
    """Every fixed regression sentence parses under the source-only schema and
    appears in a bucket-free prompt.

    This asserts prompt/parser behaviour on canned responses. It is not
    evidence about the model's real classification accuracy; see the report's
    remaining-limits section.
    """
    from pathlib import Path
    cases = json.loads((
        Path(__file__).parent / "fixtures"
        / "policy_polarity_semantic_cases_v1.json"
    ).read_text(encoding="utf-8"))
    for case in cases:
        text, classification = case["text"], case["classification"]
        payload = json.loads(analysis_json(
            classification=classification,
            review_required=classification in {"mixed", "ambiguous"}))
        payload["propositions"][0]["text"] = text
        payload["propositions"][0]["source_end"] = len(text)
        payload["propositions"][0]["classification"] = (
            "ambiguous" if classification in {"mixed", "ambiguous"}
            else classification)
        parsed = semantics.parse_source_analysis(
            json.dumps(payload, ensure_ascii=False), text)
        assert parsed["source_classification"] == classification
        if classification in {"mixed", "ambiguous"}:
            assert parsed["review_required"] is True
        prompt = semantics.build_source_prompt(text)
        assert text in prompt
        assert "BUCKET" not in prompt


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def comparison_json(
        meaning_preserved=True, review_required=False, omitted=None,
        added=None, contradiction=False):
    return json.dumps({
        "meaning_preserved": meaning_preserved,
        "omitted_propositions": omitted or [],
        "added_propositions": added or [],
        "contradiction_detected": contradiction,
        "review_required": review_required,
    }, ensure_ascii=False)


class TwoPhaseProvider(llm_providers.FixtureProvider):
    """Answers Phase A then Phase B, counting calls and recording prompts."""

    def __init__(self, source=None, comparison=None,
                 model_name="semantic-fixture-v1"):
        super().__init__(model_name=model_name)
        self._source = source if source is not None else analysis_json()
        self._comparison = (
            comparison if comparison is not None else comparison_json())
        self.calls = 0
        self.prompts = []

    def compare_text(self, prompt, prompt_version):
        self.calls += 1
        self.prompts.append(prompt)
        if prompt_version == semantics.SOURCE_PROMPT_VERSION:
            return self._result(self._source, prompt_version, {})
        return self._result(self._comparison, prompt_version, {})


def _trusted(provider):
    """A trusted-analyzer resolver standing in for deployment config."""
    def _resolve(env=None):
        return dao.TrustedAnalyzer(
            provider_name=provider.provider_name,
            model_name=provider.model_name,
            identity={
                "provider": provider.provider_name,
                "model": provider.model_name,
                "source_prompt_version": semantics.SOURCE_PROMPT_VERSION,
                "comparison_prompt_version":
                    semantics.COMPARISON_PROMPT_VERSION,
                "semantic_schema_version": semantics.SEMANTIC_SCHEMA_VERSION,
                "settings_fingerprint": semantics.settings_fingerprint(),
            },
        )
    return _resolve


def _write_normalized(isolated_dao, selector, receipt_id=None):
    span = json.loads(selector.read_text(encoding="utf-8"))["source_spans"][0]
    contract = normalized_contract(span, receipt_id)
    (dao.case_dir(CASE) / f"normalized_policy_clause_{DOC}.json").write_text(
        json.dumps(contract, ensure_ascii=False), encoding="utf-8")
    return contract


def _seed_passed_stages(make_args):
    """Record policy_clause_processing + a dependent as passed, so the cascade
    has something real to invalidate.

    Written schema-valid on purpose -- a `passed` stage must carry a
    non-null backup_path, and a fixture that skipped that would be testing a
    state the DAO could never produce.
    """
    stamp = dao.now_iso()
    state = {
        "case_id": CASE,
        "run_id": RUN,
        "updated_at": stamp,
        "human_input_status": [],
        "stages": [
            {"stage_name": name, "status": "passed",
             "started_at": stamp, "completed_at": stamp,
             "attempt_count": 1,
             "backup_path": f"outputs/{CASE}/_backups/step_{index}_{name}/"}
            for index, name in enumerate(
                ("document_processing", "policy_clause_processing",
                 "claim_analysis"), start=1)
        ],
    }
    assert dao._schema_check(state, "run_state.schema.json") == []
    dao.save_run_state(CASE, state)
