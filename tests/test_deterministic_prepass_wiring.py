"""The rule pre-pass, as the driver's reader actually invokes it.

`test_claim_analysis_deterministic` proves the rules read the page. These prove
the wiring: that a settled field is subtracted from the prompt, that a document
the rules settle entirely costs no provider call at all, and that a field with
no rule reaches the model unchanged.

The provider is a recording fake rather than a mock of the transport, because
what matters is the observable contract -- how many calls were made and which
fields each one asked for -- not the call shape.
"""
from __future__ import annotations

import claim_analysis_extraction as extraction

from test_claim_analysis_deterministic import PAGE_705


DISABILITY_FIELDS = [
    {"field_id": "documented_disability_rate", "value_shape": "number",
     "label": "Disability rate"},
    {"field_id": "documented_disability_duration", "value_shape": "text",
     "label": "Duration"},
    {"field_id": "documented_disability_standard", "value_shape": "text",
     "label": "Standard"},
]
NARRATIVE_FIELD = {"field_id": "accident_mechanism", "value_shape": "text",
                   "label": "Accident mechanism"}


class RecordingProvider:
    """Counts calls and remembers every prompt it was given."""

    def __init__(self, structured=None):
        self.prompts: list[str] = []
        self._structured = structured or {"fields": {}}

    def analyze_text_structured(self, prompt, version, schema):
        self.prompts.append(prompt)

        class _Result:
            structured_output = self._structured
            text = None

        return _Result()

    @property
    def calls(self) -> int:
        return len(self.prompts)


def _reader(provider, text=PAGE_705):
    return extraction.make_reader(provider, {"DOC_013": [{"page": 1, "text": text}]})


def test_a_fully_settled_document_costs_no_provider_call():
    """The whole point of the pre-pass.

    Every field this document was asked for is label-anchored, so there is
    nothing left to ask a model about -- and the call must not be made merely
    because the code path used to make one.
    """
    provider = RecordingProvider()

    found = _reader(provider)("DOC_013", "후유장해진단서", DISABILITY_FIELDS)

    assert provider.calls == 0
    assert found["documented_disability_rate"]["value"] == 13
    assert found["documented_disability_duration"]["value"] == "영구"


def test_an_unruled_field_still_reaches_the_model():
    """A pre-pass, not a replacement.

    `accident_mechanism` is deliberately never rule-extracted, so its presence
    must still buy a provider call.
    """
    provider = RecordingProvider()

    _reader(provider)("DOC_013", "후유장해진단서",
                      DISABILITY_FIELDS + [NARRATIVE_FIELD])

    assert provider.calls == 1


def test_settled_fields_are_removed_from_the_prompt():
    """The saving is in the prompt, not only in the call count.

    A settled field left in the field list would still be described, still
    consume tokens, and still invite a second, conflicting reading.
    """
    provider = RecordingProvider()

    _reader(provider)("DOC_013", "후유장해진단서",
                      DISABILITY_FIELDS + [NARRATIVE_FIELD])
    prompt = provider.prompts[0]

    assert "accident_mechanism" in prompt
    for row in DISABILITY_FIELDS:
        assert row["field_id"] not in prompt


def test_deterministic_readings_survive_alongside_model_readings():
    """Both halves land in one result dict, in the same shape."""
    provider = RecordingProvider({"fields": {
        "accident_mechanism": {
            "presence": "asserted",
            "value": "계단에서 넘어짐",
            "page": 1,
            "quote": "상기 환자 상기 일, 환자 계단에서 넘어지며 수상(환자진술)",
        },
    }})

    found = _reader(provider)("DOC_013", "후유장해진단서",
                              DISABILITY_FIELDS + [NARRATIVE_FIELD])

    assert found["accident_mechanism"]["value"] == "계단에서 넘어짐"
    assert found["documented_disability_rate"]["value"] == 13
    assert found["documented_disability_rate"]["presence"] == "asserted"


def test_a_page_without_the_form_falls_through_entirely():
    """No rule fires, so the document is read exactly as it was before."""
    provider = RecordingProvider()

    _reader(provider, text="영상판독지\n판독소견: 골절 소견 없음\n")(
        "DOC_013", "영상판독지", DISABILITY_FIELDS)

    assert provider.calls == 1
    for row in DISABILITY_FIELDS:
        assert row["field_id"] in provider.prompts[0]
