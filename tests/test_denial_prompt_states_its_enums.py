"""The prompt must name the values the transport schema cannot.

`_transport_schema()` deliberately declares `denial_reasons` and
`accepted_coverages` as bare `{"type": "object"}`: the fully materialized
denial schema exceeds Windows' argv limit, and Claude accepts `--json-schema`
only inline on argv. The nested item shapes are therefore enforced ONLY
locally, after the model has answered.

That is fine for structure, which the one correction round can repair. It is
not fine for closed vocabularies: the model is never shown that
`decision_type` is `denial`/`reduction`, and the only enum it IS shown is
`reviewer_role`, whose values are Korean. Against a Korean source bundle that
is a one-way pull.

CASE_048, 2026-08-20: the model returned a Korean phrase for `decision_type`
on the initial call AND on the correction retry, so the driver hit P4's halt
and `denial_response` failed twice with

    '<Korean text>' is not one of ['denial', 'reduction']

These tests pin that every closed vocabulary the transport schema drops is
stated in the prompt instead, and that the prompt's list stays in step with
the schema -- a new enum value that never reaches the prompt is the same
defect again.
"""
from __future__ import annotations

import run_denial_response_driver as driver


def _prompt_text() -> str:
    bundle = [{"document_id": "DOC_007", "document_type": "insurer_response",
               "pages": [{"page": 1, "text": "보상하지 않습니다"}]}]
    return driver._prompt(bundle)


def _item_enum(collection: str, field: str) -> list[str]:
    body = driver._body_schema()
    return body["properties"][collection]["items"]["properties"][field]["enum"]


def test_the_prompt_states_the_decision_type_values():
    """The exact defect: an English enum, unstated, against Korean sources.

    Asserted as a stated ALLOWED SET, not as bare substrings: "denial" and
    "reduction" both occur in this prompt as ordinary English words, so a
    substring check passed while the model was still never told they are the
    only two legal values.
    """
    prompt = _prompt_text()
    values = _item_enum("denial_reasons", "decision_type")
    line = next((l for l in prompt.splitlines()
                 if "decision_type" in l and "one of" in l), None)
    assert line is not None, "no line states decision_type's allowed values"
    for value in values:
        assert value in line, value


def test_the_prompt_states_the_payment_status_values():
    prompt = _prompt_text()
    for value in _item_enum("denial_reasons", "payment_status"):
        assert value in prompt, value


def test_the_prompt_says_these_are_verbatim_codes_not_translations():
    """Naming the values is not enough on its own -- the model has to be told
    they are literals, or a Korean bundle invites a Korean rendering of them."""
    prompt = _prompt_text()
    assert "exactly as written" in prompt or "verbatim" in prompt


def test_every_dropped_enum_is_covered():
    """The guard against this recurring: any closed vocabulary inside the two
    collections the transport schema declares as bare objects must appear in
    the prompt. A new enum value added to the schema fails here until the
    prompt names it too."""
    body = driver._body_schema()
    prompt = _prompt_text()
    missing = []
    for collection in ("denial_reasons", "accepted_coverages"):
        properties = body["properties"][collection]["items"]["properties"]
        for field, spec in properties.items():
            if not isinstance(spec, dict) or "enum" not in spec:
                continue
            # taxonomy_code is 22 R-codes with their own codebook section; it
            # is addressed by the prompt's Top-3 candidate instruction and the
            # taxonomy reference, not by listing every code inline.
            if field == "taxonomy_code":
                continue
            for value in spec["enum"]:
                if value not in prompt:
                    missing.append(f"{collection}.{field}={value}")
    assert not missing, "enum values absent from the prompt: " + ", ".join(missing)
