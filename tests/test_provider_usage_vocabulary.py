"""A receipt records the same token facts whichever provider reported them.

`_normalize_usage` was written against the Anthropic vocabulary
(`input_tokens` / `output_tokens` / `cache_read_input_tokens` /
`output_tokens_details.thinking_tokens`). An OpenAI-compatible backend --
openrouter, openai-api -- reports `prompt_tokens` / `completion_tokens` /
`prompt_tokens_details.cached_tokens` /
`completion_tokens_details.reasoning_tokens`, so once openrouter became the
default every driver receipt kept `total_tokens` and dropped the rest. The
receipt schema itself closes `additionalProperties`, so the fix needed the
contract to move too, not just the code.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import driver_runtime  # noqa: E402
from _validation import load_registry, validate_instance  # noqa: E402


OPENROUTER_USAGE = {
    "prompt_tokens": 1200,
    "completion_tokens": 340,
    "total_tokens": 1540,
    "prompt_tokens_details": {"cached_tokens": 900, "cache_write_tokens": 50},
    "completion_tokens_details": {"reasoning_tokens": 120},
    "cost": 0.0031,
}
ANTHROPIC_USAGE = {
    "input_tokens": 1200,
    "output_tokens": 340,
    "cache_read_input_tokens": 900,
    "cache_creation_input_tokens": 50,
    "output_tokens_details": {"thinking_tokens": 120},
}


def test_the_openai_compatible_vocabulary_is_renamed_not_dropped():
    assert driver_runtime._normalize_usage(OPENROUTER_USAGE) == {
        "input_tokens": 1200,
        "output_tokens": 340,
        "cache_read_input_tokens": 900,
        "cache_creation_input_tokens": 50,
        "total_tokens": 1540,
        "thinking_tokens": 120,
        "cost_usd": 0.0031,
    }


def test_the_anthropic_vocabulary_is_unchanged():
    # The path that already worked must keep working byte for byte.
    assert driver_runtime._normalize_usage(ANTHROPIC_USAGE) == {
        "input_tokens": 1200,
        "output_tokens": 340,
        "cache_read_input_tokens": 900,
        "cache_creation_input_tokens": 50,
        "thinking_tokens": 120,
    }


def test_both_providers_produce_the_same_record_from_the_same_call():
    # The point of normalizing: two runs on different backends are comparable.
    a = driver_runtime._normalize_usage(OPENROUTER_USAGE)
    b = driver_runtime._normalize_usage(ANTHROPIC_USAGE)
    shared = set(a) & set(b)
    assert {k: a[k] for k in shared} == {k: b[k] for k in shared}


def test_a_canonical_key_is_never_overwritten_by_an_alias():
    # A provider reporting both keeps the one it named directly.
    usage = {"input_tokens": 10, "prompt_tokens": 999}
    assert driver_runtime._normalize_usage(usage)["input_tokens"] == 10


@pytest.mark.parametrize("usage", [
    {"prompt_tokens": "lots"},
    {"prompt_tokens": True},          # bool is an int subclass; not a count
    {"prompt_tokens": -5},
    {"prompt_tokens_details": "cached"},
    {"completion_tokens_details": {"reasoning_tokens": None}},
    {"cost": -1},
    {"cost": True},
    None,
    "not a mapping",
])
def test_an_unusable_figure_is_left_missing_never_zeroed(usage):
    # A missing figure must stay distinguishable from a measured one; a
    # fabricated 0 would read as "the provider reported none".
    assert driver_runtime._normalize_usage(usage) == {}


def test_a_receipt_carrying_openrouter_usage_validates_against_the_contract():
    # provider_usage closes additionalProperties, so this is the test that
    # would have failed before the schema moved.
    receipt = driver_runtime.make_receipt(
        case_id="CASE_999", run_id="RUN_20260825_001", stage="claim_analysis",
        unit_id="DOC_001", input_digests={"a": "0" * 64},
        prompt_version="v0.1", response_schema_version="v0.1",
        provider_name="openrouter", model_name="anthropic/claude-sonnet-4",
        completed_contracts=[], provider_usage=OPENROUTER_USAGE)

    schemas, registry = load_registry()
    assert validate_instance(receipt, "driver_receipt.schema.json",
                             schemas, registry) == []
    assert receipt["provider_usage"]["input_tokens"] == 1200
    assert receipt["provider_usage"]["cost_usd"] == 0.0031


def test_a_vendor_prefixed_model_slug_survives_the_receipt_contract():
    # OpenRouter ids carry a '/', which a stricter pattern would have refused.
    receipt = driver_runtime.make_receipt(
        case_id="CASE_999", run_id="RUN_20260825_001", stage="claim_analysis",
        unit_id="DOC_001", input_digests={"a": "0" * 64},
        prompt_version="v0.1", response_schema_version="v0.1",
        provider_name="openrouter", model_name="google/gemini-2.5-pro",
        completed_contracts=[])

    schemas, registry = load_registry()
    assert validate_instance(receipt, "driver_receipt.schema.json",
                             schemas, registry) == []
