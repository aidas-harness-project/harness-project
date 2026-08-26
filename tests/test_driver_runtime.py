import hashlib
import json
from types import SimpleNamespace

import dao
import driver_runtime
import trace as trace_mod


CASE = "CASE_009"
RUN = "RUN_20260813_001"


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def test_receipt_fingerprint_is_stable_and_invalidates_on_input_change():
    first = {"manifest": _digest("manifest"), "text": _digest("text")}
    reordered = {"text": _digest("text"), "manifest": _digest("manifest")}
    receipt = driver_runtime.make_receipt(
        case_id=CASE, run_id=RUN, stage="denial_response", unit_id="initial_extraction",
        input_digests=first, prompt_version="denial_initial_v1",
        response_schema_version="denial_initial_response_v1", provider_name="fixture",
        model_name="fixture", completed_contracts=["denial_reason_result.json"],
    )

    assert driver_runtime.input_fingerprint(first) == driver_runtime.input_fingerprint(reordered)
    assert driver_runtime.receipt_matches(
        receipt, input_digests=reordered, prompt_version="denial_initial_v1",
        response_schema_version="denial_initial_response_v1", provider_name="fixture",
        model_name="fixture",
    )
    assert not driver_runtime.receipt_matches(
        receipt, input_digests={"manifest": _digest("changed")},
        prompt_version="denial_initial_v1", response_schema_version="denial_initial_response_v1",
        provider_name="fixture", model_name="fixture",
    )


def test_dao_writes_and_reads_schema_valid_driver_receipt(
    isolated_dao, make_args, capsys, tmp_path
):
    payload = driver_runtime.make_receipt(
        case_id=CASE, run_id=RUN, stage="denial_response", unit_id="initial_extraction",
        input_digests={"manifest": _digest("manifest")},
        prompt_version="denial_initial_v1", response_schema_version="denial_initial_response_v1",
        provider_name="fixture", model_name="fixture",
        completed_contracts=["denial_reason_result.json"],
    )
    data_file = tmp_path / "receipt.json"
    data_file.write_text(json.dumps(payload), encoding="utf-8")
    args = make_args(case_id=CASE, run_id=RUN, stage="denial_response",
                     unit_id="initial_extraction", data_file=str(data_file))

    assert dao.cmd_write_driver_receipt(args) == 0
    capsys.readouterr()
    assert dao.cmd_read_driver_receipt(make_args(
        case_id=CASE, run_id=RUN, stage="denial_response", unit_id="initial_extraction"
    )) == 0
    assert json.loads(capsys.readouterr().out)["input_fingerprint"] == payload["input_fingerprint"]


def test_dao_refuses_receipt_identity_mismatch(isolated_dao, make_args, capsys, tmp_path):
    payload = driver_runtime.make_receipt(
        case_id=CASE, run_id=RUN, stage="denial_response", unit_id="initial_extraction",
        input_digests={"manifest": _digest("manifest")},
        prompt_version="denial_initial_v1", response_schema_version="denial_initial_response_v1",
        provider_name="fixture", model_name="fixture", completed_contracts=[],
    )
    payload["unit_id"] = "another_unit"
    data_file = tmp_path / "bad-receipt.json"
    data_file.write_text(json.dumps(payload), encoding="utf-8")

    assert dao.cmd_write_driver_receipt(make_args(
        case_id=CASE, run_id=RUN, stage="denial_response", unit_id="initial_extraction",
        data_file=str(data_file)
    )) == 1
    assert "identity" in capsys.readouterr().out


def test_driver_span_hashes_unit_id_without_recording_it(monkeypatch, tmp_path):
    trace_mod.reset()
    monkeypatch.delenv("HARNESS_TRACE", raising=False)
    with driver_runtime.driver_span(
        CASE, RUN, "input_snapshot", unit_id="DOC_001", trace_root=tmp_path / "outputs"
    ):
        pass
    trace_mod._close_handles()
    records = []
    for shard in (tmp_path / "outputs").glob("**/*.jsonl"):
        records.extend(json.loads(line) for line in shard.read_text(encoding="utf-8").splitlines())
    assert len(records) == 1
    assert records[0]["attrs"]["unit_hash"] == _digest("DOC_001")
    assert "DOC_001" not in json.dumps(records[0])
    trace_mod.reset()


def test_structured_output_gets_exactly_one_driver_owned_correction():
    class Provider:
        def __init__(self):
            self.calls = 0

        def analyze_text_structured(self, prompt, prompt_version, output_schema):
            self.calls += 1
            return SimpleNamespace(structured_output={"ok": self.calls == 2})

    provider = Provider()
    value = driver_runtime.structured_with_one_correction(
        provider=provider, prompt="synthetic", prompt_version="v1", output_schema={"type": "object"},
        validate=lambda result: result if result["ok"] else (_ for _ in ()).throw(ValueError("not ok")),
    )

    assert value == {"ok": True}
    assert provider.calls == 2


# --- provider token counts are recorded, never derived -----------------------

class _UsageResult:
    def __init__(self, output, usage=None):
        self.structured_output = output
        self.raw_metadata = {"usage": usage} if usage is not None else {}


class _UsageProvider:
    provider_name, model_name = "claude-cli", "claude-sonnet-5"

    def __init__(self, *results):
        self._results = list(results)
        self.calls = 0

    def analyze_text_structured(self, prompt, prompt_version, output_schema):
        self.calls += 1
        return self._results.pop(0)


def test_call_usage_copies_scalar_counters_and_lifts_thinking_tokens():
    """Copied verbatim from the provider's own report. The nested per-model and
    per-iteration breakdowns are CLI internals and are dropped; thinking tokens
    are lifted out of `output_tokens_details` because they are a load-bearing
    counter for explaining a slow call."""
    result = _UsageResult({}, {
        "input_tokens": 2, "output_tokens": 52,
        "cache_creation_input_tokens": 26796, "cache_read_input_tokens": 0,
        "output_tokens_details": {"thinking_tokens": 1234},
        "service_tier": "standard",
        "model_usage": {"claude-sonnet-5": {"inputTokens": 2}},
    })
    usage = driver_runtime.call_usage(result)
    assert usage["input_tokens"] == 2
    assert usage["cache_creation_input_tokens"] == 26796
    assert usage["thinking_tokens"] == 1234
    assert "model_usage" not in usage, "nested CLI internals must not be persisted"
    assert "service_tier" not in usage, "non-numeric fields are not counters"


def test_call_usage_returns_none_when_the_provider_reported_nothing():
    """No usage is 'not measured', which must stay distinguishable from zero."""
    assert driver_runtime.call_usage(_UsageResult({})) is None
    assert driver_runtime.call_usage(object()) is None


def test_both_calls_are_recorded_when_p4_correction_runs():
    """A run that needed the correction paid for TWO calls; reporting them as
    one would understate the cost of a stage that had to be corrected."""
    usage_out = []
    provider = _UsageProvider(
        _UsageResult({"bad": True}, {"output_tokens": 10}),
        _UsageResult({"ok": True}, {"output_tokens": 20}),
    )

    def validate(value):
        if value.get("bad"):
            raise ValueError("nope")
        return value

    out = driver_runtime.structured_with_one_correction(
        provider=provider, prompt="p", prompt_version="v", output_schema={},
        validate=validate, usage_out=usage_out)

    assert out == {"ok": True}
    assert provider.calls == 2
    assert [entry["output_tokens"] for entry in usage_out] == [10, 20]


def test_usage_is_outside_the_reuse_fingerprint():
    """Recording a token count must never change whether a candidate can be
    reused -- otherwise measuring a run would invalidate it."""
    common = dict(case_id="CASE_9001", run_id="RUN_1", stage="claim_analysis",
                  unit_id="m1", candidate_id="c1", input_digests={"a": "0" * 64},
                  prompt_version="v", response_schema_version="s",
                  provider_name="claude-cli", model_name="claude-sonnet-5",
                  result={"x": 1})
    without = driver_runtime.make_candidate(**common)
    with_usage = driver_runtime.make_candidate(**common,
                                               call_usage=[{"output_tokens": 99}])
    assert with_usage["call_usage"] == [{"output_tokens": 99}]
    assert with_usage["input_fingerprint"] == without["input_fingerprint"]
    assert "call_usage" not in without, "an unmeasured run records no empty field"
