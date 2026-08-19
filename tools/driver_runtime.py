"""Small shared, non-semantic helpers for the three stage drivers.

Drivers keep their prompts, candidate parsing, and merge rules locally. This
module only makes their receipt fingerprints and measurement vocabulary
consistent; it never opens case data or writes a governed artifact itself.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, TypeVar

import trace as trace_mod


_PHASES = {
    "input_snapshot": "compute",
    "dao_content_read": "io",
    "provider_wait": "provider",
    "evidence_verify": "compute",
    "deterministic_merge": "compute",
    "dao_publish": "io",
}

T = TypeVar("T")


def input_fingerprint(input_digests: Mapping[str, str]) -> str:
    """Stable SHA-256 fingerprint of named, already-computed input digests."""
    normalized = dict(sorted(input_digests.items()))
    if not normalized:
        raise ValueError("input_digests must not be empty")
    for name, digest in normalized.items():
        if not name or not isinstance(digest, str) or len(digest) != 64:
            raise ValueError(f"invalid input digest for {name!r}")
        int(digest, 16)
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def make_receipt(*, case_id: str, run_id: str, stage: str, unit_id: str,
                 input_digests: Mapping[str, str], prompt_version: str,
                 response_schema_version: str, provider_name: str,
                 model_name: str, completed_contracts: list[str],
                 status: str = "complete",
                 provider_usage: Mapping[str, Any] | None = None) -> dict:
    """Build a receipt payload for DAO `write-driver-receipt` validation.

    `provider_usage` is the provider's OWN token counts for this unit's call,
    copied verbatim. It is optional because not every provider reports usage,
    and an absent figure must stay distinguishable from an invented one -- the
    same rule T13 applies to `record-dispatch`. Omitting it costs the ability to
    explain a unit's wall time at all: across CASE_302~321 M1 ranged 117s-492s
    and M2 64s-564s with nothing recorded to test an output-volume hypothesis
    against, which left the question answerable only by speculation.
    """
    if status not in {"complete", "in_progress"}:
        raise ValueError("status must be complete or in_progress")
    digests = dict(sorted(input_digests.items()))
    receipt_usage = _normalize_usage(provider_usage)
    return {
        "case_id": case_id,
        "run_id": run_id,
        "stage": stage,
        "unit_id": unit_id,
        "input_fingerprint": input_fingerprint(digests),
        "input_digests": digests,
        "prompt_version": prompt_version,
        "response_schema_version": response_schema_version,
        "provider_name": provider_name,
        "model_name": model_name,
        "completed_contracts": sorted(completed_contracts),
        "status": status,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        **({"provider_usage": receipt_usage} if receipt_usage else {}),
    }


_USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens",
               "cache_creation_input_tokens", "total_tokens")


def _normalize_usage(usage: Mapping[str, Any] | None) -> dict[str, int]:
    """Pull the integer token counts out of a provider usage mapping.

    Copies only keys the receipt schema declares and only when the value is a
    real non-boolean integer, so a provider that reports a partial or oddly
    shaped usage block yields fewer fields rather than a fabricated zero.
    `thinking_tokens` is nested under `output_tokens_details` in the claude-cli
    envelope, so it is lifted explicitly.
    """
    if not isinstance(usage, Mapping):
        return {}
    out: dict[str, int] = {}
    for key in _USAGE_KEYS:
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            out[key] = value
    details = usage.get("output_tokens_details")
    if isinstance(details, Mapping):
        thinking = details.get("thinking_tokens")
        if isinstance(thinking, int) and not isinstance(thinking, bool) and thinking >= 0:
            out["thinking_tokens"] = thinking
    return out


def receipt_matches(receipt: Mapping[str, object], *, input_digests: Mapping[str, str],
                    prompt_version: str, response_schema_version: str,
                    provider_name: str, model_name: str) -> bool:
    """Whether a completed unit may be reused without opening case files."""
    return (
        receipt.get("status") == "complete"
        and receipt.get("input_fingerprint") == input_fingerprint(input_digests)
        and receipt.get("prompt_version") == prompt_version
        and receipt.get("response_schema_version") == response_schema_version
        and receipt.get("provider_name") == provider_name
        and receipt.get("model_name") == model_name
    )


def make_candidate(*, case_id: str, run_id: str, stage: str, unit_id: str,
                   candidate_id: str, input_digests: Mapping[str, str],
                   prompt_version: str, response_schema_version: str,
                   provider_name: str, model_name: str,
                   result: Mapping[str, Any],
                   call_usage: list | None = None) -> dict:
    """Build one completed sub-unit payload for DAO `write-driver-candidate`.

    `input_digests` must name only what THIS unit consumed. A stage-wide digest
    set would make every unit's fingerprint change when any one document's
    redacted text changes, which would defeat the point of per-unit resume.

    `call_usage` is the provider's own per-call token counts (see `call_usage`).
    It is diagnostic only and deliberately outside `input_fingerprint`, so a
    recorded count never changes whether a candidate can be reused.
    """
    digests = dict(sorted(input_digests.items()))
    usage = [dict(entry) for entry in (call_usage or []) if entry]
    return {
        **({"call_usage": usage} if usage else {}),
        "case_id": case_id,
        "run_id": run_id,
        "stage": stage,
        "unit_id": unit_id,
        "candidate_id": candidate_id,
        "input_fingerprint": input_fingerprint(digests),
        "input_digests": digests,
        "prompt_version": prompt_version,
        "response_schema_version": response_schema_version,
        "provider_name": provider_name,
        "model_name": model_name,
        "status": "complete",
        "result": dict(result),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }


def candidate_matches(candidate: Mapping[str, object], *,
                      input_digests: Mapping[str, str], prompt_version: str,
                      response_schema_version: str, provider_name: str,
                      model_name: str) -> bool:
    """Whether one completed sub-unit may be reused without a provider call."""
    return (
        candidate.get("status") == "complete"
        and isinstance(candidate.get("result"), dict)
        and candidate.get("input_fingerprint") == input_fingerprint(input_digests)
        and candidate.get("prompt_version") == prompt_version
        and candidate.get("response_schema_version") == response_schema_version
        and candidate.get("provider_name") == provider_name
        and candidate.get("model_name") == model_name
    )


class ProviderInterrupted(RuntimeError):
    """A provider call was stopped at an explicit, test-owned interruption point.

    Raised only by `interruption_hook`, which is unset in production. It exists
    so a resume test can stop the driver at a named unit deterministically,
    rather than by exhausting tokens or killing the process -- neither of which
    can target a specific unit, and both of which leave the test unable to
    state which units had actually completed.
    """


# Test-only seam. Production leaves this None, so `_maybe_interrupt` compiles
# to a single attribute check on the hot path and no interruption is reachable.
interruption_hook: Callable[[str, str], None] | None = None


def maybe_interrupt(stage: str, candidate_id: str) -> None:
    """Consult the test-only interruption seam before a provider call.

    Placed immediately before the provider round trip and after reuse is
    decided, so a test that interrupts unit N observes exactly the units the
    driver really would have completed before N.
    """
    hook = interruption_hook
    if hook is not None:
        hook(stage, candidate_id)


@contextlib.contextmanager
def interrupt_at(stage: str, candidate_ids: Iterable[str]) -> Iterator[list[str]]:
    """Test helper: raise ProviderInterrupted at the named units.

    Yields the list of units that reached the seam, so a test can assert which
    provider calls were actually attempted rather than inferring it.
    """
    targets = set(candidate_ids)
    reached: list[str] = []
    previous = interruption_hook

    def hook(hook_stage: str, candidate_id: str) -> None:
        reached.append(candidate_id)
        if candidate_id in targets:
            raise ProviderInterrupted(
                f"test interruption at {hook_stage}/{candidate_id}")

    globals()["interruption_hook"] = hook
    try:
        yield reached
    finally:
        globals()["interruption_hook"] = previous


def call_usage(result: Any) -> dict | None:
    """The provider's own token counts for one call, or None if it reported none.

    Copied verbatim from `raw_metadata['usage']`, never derived: a count this
    driver computed would be indistinguishable from one the provider measured.
    Only scalar counters are kept -- the nested per-model and per-iteration
    breakdowns are the CLI's internals, and a prompt or document text can never
    reach a usage field, so this stays safe to persist beside a candidate.

    Why it is worth persisting: CASE_047's M1 ran the SAME input six times at
    159.6 / 179.5 / 271.4 / 397.5 / 502.1 / 666.1s (2026-08-18). Input size,
    document count and output size were each ruled out by measurement -- the
    three cases compared had near-identical outputs (46-56 fields, 62-65
    evidence references) and 5.6x different wall times. Cache hits and thinking
    tokens are the remaining candidates and were simply not being recorded.
    """
    metadata = getattr(result, "raw_metadata", None)
    if not isinstance(metadata, Mapping):
        return None
    usage = metadata.get("usage")
    if not isinstance(usage, Mapping):
        return None
    kept = {key: value for key, value in usage.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)}
    details = usage.get("output_tokens_details")
    if isinstance(details, Mapping):
        thinking = details.get("thinking_tokens")
        if isinstance(thinking, int):
            kept["thinking_tokens"] = thinking
    return kept or None


def structured_with_one_correction(*, provider: Any, prompt: str, prompt_version: str,
                                  output_schema: Mapping[str, Any],
                                  validate: Callable[[Mapping[str, Any]], T],
                                  usage_out: list | None = None) -> T:
    """Validate one structured response, then make exactly one correction call.

    Native provider schemas constrain JSON shape, but a stage's tighter
    candidate rules still require driver-owned validation.  This helper makes
    P4's single correction explicit without logging a prompt, source text, or
    model output.  A second failure is re-raised for the orchestrator to halt.

    `usage_out`, when given, collects one `call_usage` mapping per provider
    call -- so a run that needed the correction records BOTH calls rather than
    reporting the pair as one. Caller-owned list, following the same
    out-parameter idiom `undecided`/`judged` use in segmentation.
    """
    def record(call_result):
        if usage_out is None:
            return
        usage = call_usage(call_result)
        if usage is not None:
            usage_out.append(usage)

    result = provider.analyze_text_structured(prompt, prompt_version, output_schema)
    record(result)
    if not isinstance(result.structured_output, dict):
        raise ValueError("provider returned no structured object")
    try:
        return validate(result.structured_output)
    except Exception as first_error:
        correction = (
            f"{prompt}\n\nYour previous JSON failed validation: {first_error}. "
            "Return a corrected JSON object that satisfies the same schema."
        )
        retry = provider.analyze_text_structured(correction, prompt_version, output_schema)
        record(retry)
        if not isinstance(retry.structured_output, dict):
            raise ValueError("provider correction returned no structured object") from first_error
        return validate(retry.structured_output)


@contextlib.contextmanager
def driver_span(case_id: str, run_id: str, phase: str, *, unit_id: str,
                worker_count: int | None = None, items: int | None = None,
                trace_root: Path | None = None) -> Iterator:
    """Emit one privacy-safe, consistently named driver span."""
    if phase not in _PHASES:
        raise ValueError(f"unknown driver phase: {phase}")
    trace_mod.configure(case_id, run_id, root=trace_root)
    attrs = {"unit_hash": hashlib.sha256(unit_id.encode("utf-8")).hexdigest()}
    if worker_count is not None:
        attrs["worker_count"] = worker_count
    if items is not None:
        attrs["items"] = items
    with trace_mod.span(f"driver.{phase}", category=_PHASES[phase],
                        case_id=case_id, **attrs) as span:
        yield span
