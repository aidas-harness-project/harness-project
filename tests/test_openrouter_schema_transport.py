"""Static verification of every JSON Schema the harness can send to OpenRouter.

No key is needed for any of this and none of it calls out: the questions are
whether a schema survives the transformation the provider applies
(`_cli_json_schema`), whether that transformation loses anything beyond the two
keywords it declares, whether the result is still a valid schema, and whether
it encodes and decodes byte-faithfully as the request body would carry it.

Scope note: the transport schemas were shaped by CLI constraints that do NOT
apply here -- claude-cli's 8,000-char inline-argv cap and its ajv STRICT mode,
which refuses `{"type": ["integer", "null"]}`. Over HTTP the schema rides in
the JSON body and the function-parameters validator is permissive, so those
shapes are more constrained than this transport requires. That is the safe
direction and the tests below assert it rather than assuming it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

import claim_analysis_extraction
import driver_schema
import llm_providers as providers
import run_denial_response_driver
import segment_case

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS_DIR = ROOT / "schemas"

# The schemas that genuinely reach a provider as `output_schema` today.
SENT_SCHEMAS = {
    "segment_contact_sheet": segment_case.SEGMENT_OUTPUT_SCHEMA,
    "denial_transport": run_denial_response_driver._transport_schema(),
    "claim_analysis_transport": claim_analysis_extraction.output_schema([]),
}

# A request body carries the schema on EVERY structured call, so an oversized
# one is paid for per document, not once. Well above the largest real transport
# schema and far below anything that would trouble an HTTP body.
_TRANSPORT_SCHEMA_BUDGET_CHARS = 32_000


def prune(schema):
    return providers._cli_json_schema(schema)


def walk(node, path="$"):
    if isinstance(node, dict):
        for key, value in node.items():
            yield path, key, value
            yield from walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from walk(value, f"{path}[{index}]")


@pytest.mark.parametrize("name", sorted(SENT_SCHEMAS))
def test_a_sent_schema_is_still_a_valid_schema_after_pruning(name):
    # Pruning walks and rebuilds every node. A schema that came out invalid
    # would be rejected by the provider with a message about our request, not
    # about the model, and only on the first real call.
    Draft202012Validator.check_schema(prune(SENT_SCHEMAS[name]))


@pytest.mark.parametrize("name", sorted(SENT_SCHEMAS))
def test_pruning_removes_the_two_declared_keywords_and_nothing_else(name):
    original = SENT_SCHEMAS[name]
    pruned = prune(original)

    def strip(node):
        if isinstance(node, dict):
            return {k: strip(v) for k, v in node.items()
                    if k not in providers._CLI_SCHEMA_UNSUPPORTED_KEYWORDS}
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node

    # An independent re-derivation, not a re-run of the function under test:
    # if pruning ever dropped a third keyword, this equality is what fails.
    assert pruned == strip(original)
    assert not [k for _, k, _ in walk(pruned)
                if k in providers._CLI_SCHEMA_UNSUPPORTED_KEYWORDS]


@pytest.mark.parametrize("name", sorted(SENT_SCHEMAS))
def test_a_sent_schema_round_trips_through_the_request_encoding(name):
    pruned = prune(SENT_SCHEMAS[name])
    # ensure_ascii=False is what puts real UTF-8 on the wire; the denial
    # transport schema's enum is Korean (손해사정사 / 의사 / 법률전문가), so a
    # schema encoded the other way would arrive as escapes and the model would
    # be constrained to literals nobody wrote.
    encoded = json.dumps(pruned, ensure_ascii=False).encode("utf-8")
    assert json.loads(encoded.decode("utf-8")) == pruned
    assert len(encoded.decode("utf-8")) <= _TRANSPORT_SCHEMA_BUDGET_CHARS


@pytest.mark.parametrize("name", sorted(SENT_SCHEMAS))
def test_a_sent_schema_names_no_reference_the_provider_would_have_to_fetch(name):
    # A `$ref` we cannot resolve locally is an outbound reference: it either
    # fails validation at the provider or asks something on the far side to go
    # and fetch a URL. Fragment-only refs resolve inside the object we send and
    # are fine; anything else must be materialized before it leaves.
    for path, key, value in walk(prune(SENT_SCHEMAS[name])):
        if key == "$ref":
            assert isinstance(value, str) and value.startswith("#"), (
                f"{name} carries a non-fragment $ref at {path}: {value!r}")


def test_the_korean_enum_survives_all_the_way_into_the_request_body(monkeypatch):
    # End to end for the one schema whose content is Korean: build the real
    # transport schema, send it, and read what urllib would have transmitted.
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({
                "id": "gen-1",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": None,
                                "tool_calls": [{
                                    "id": "c1", "type": "function",
                                    "function": {
                                        "name": "emit_result",
                                        "arguments": json.dumps(
                                            {"status": "success",
                                             "reviewer_role": "손해사정사"},
                                            ensure_ascii=False),
                                    }}]},
                    "finish_reason": "tool_calls",
                }],
            }, ensure_ascii=False).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["body"] = request.data
        return FakeResponse()

    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)
    provider = providers.build_provider(
        providers.ProviderConfig(provider_name="openrouter"),
        env={"OPENROUTER_API_KEY": "k", "OPENROUTER_MODEL": "vendor/m"})

    schema = run_denial_response_driver._transport_schema()
    result = provider.analyze_text_structured("prompt", "v0.1", schema)

    assert "손해사정사".encode("utf-8") in captured["body"]
    sent = json.loads(captured["body"].decode("utf-8"))
    assert sent["tools"][0]["function"]["parameters"] == prune(schema)
    assert result.structured_output["reviewer_role"] == "손해사정사"


def test_every_repository_schema_a_driver_could_materialize_stays_sendable():
    """A sweep over the whole contract set, not just today's three.

    Any of these can become a transport schema the day a driver decides to send
    it. Materializing surfaces the repository-local `$ref`s that a provider
    cannot resolve; pruning and validating then proves the result is something
    this transport could carry.
    """
    checked = 0
    for path in sorted(SCHEMAS_DIR.glob("*.schema.json")):
        try:
            materialized = driver_schema.load_materialized_schema(path.name)
        except driver_schema.LocalSchemaReferenceError:
            # A schema reaching outside the repository is refused by design;
            # that refusal is the tested behaviour of driver_schema itself.
            continue
        pruned = prune(materialized)
        Draft202012Validator.check_schema(pruned)
        for node_path, key, value in walk(pruned):
            if key == "$ref":
                assert isinstance(value, str) and value.startswith("#"), (
                    f"{path.name} still carries an unresolved $ref at "
                    f"{node_path}: {value!r}")
        json.loads(json.dumps(pruned, ensure_ascii=False))
        checked += 1
    # Guard against the sweep silently checking nothing.
    assert checked >= 30, f"only {checked} schemas materialized"


def test_no_sent_schema_relies_on_a_keyword_pruning_would_silently_drop():
    """`dependentRequired` is a REAL constraint and pruning removes it.

    Harmless while nothing uses it, and a silent weakening the day something
    does -- the provider stops pre-checking it and only the local
    validate_instance() gate still applies. This is the alarm for that day.
    """
    offenders = [
        (name, path) for name, schema in SENT_SCHEMAS.items()
        for path, key, _ in walk(schema) if key == "dependentRequired"
    ]
    assert not offenders, (
        "a sent schema uses dependentRequired, which _cli_json_schema strips "
        f"before transmission: {offenders}")
