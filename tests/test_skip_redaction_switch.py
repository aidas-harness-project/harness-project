"""The development switch that skips the redaction MODEL.

Why the switch exists: the PoC corpus arrived already de-identified, the
production target is a local de-identification model (`open-decisions.md` #1),
and the LLM path measurably dominates Stage 2 once it runs on everything -- on
CASE_964 all 367 pages took it (5644s of accumulated provider time, 245s of
wall) because classification had moved AFTER redaction, so
`NoPiiClassRedactor`'s `document_type` test could not see a type yet and the
323-page policy exemption never fired.

What these pin is the boundary of the switch, because the failure mode of
getting it wrong is a silent privacy regression rather than a broken test:

  * it skips the MODEL, never the deterministic residual-PII scan;
  * it is off by default and needs an explicit opt-in;
  * an explicit `--redact` always beats the environment, so an evaluation run
    can force real redaction inside a dev shell;
  * it reaches BOTH checkpoint-2 passes (top-level documents and split
    children), since a switch that reached one would redact half a case under
    a different policy;
  * the artifact says which redactor ran, so a run is identifiable after the
    fact as not privacy-preserving.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import redact_document as rd  # noqa: E402
from redaction import DevNoLlmRedactor, RedactionLeakError  # noqa: E402


class TestPrecedence:
    def test_off_by_default(self, monkeypatch) -> None:
        monkeypatch.delenv(rd.SKIP_REDACTION_ENV, raising=False)
        assert rd.resolve_skip_redaction(None) is False

    @pytest.mark.parametrize("raw", ["1", "true", "yes", "on", "TRUE", "On"])
    def test_env_turns_it_on(self, monkeypatch, raw: str) -> None:
        monkeypatch.setenv(rd.SKIP_REDACTION_ENV, raw)
        assert rd.resolve_skip_redaction(None) is True

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off", "", "garbage"])
    def test_other_env_values_leave_it_off(self, monkeypatch, raw: str) -> None:
        monkeypatch.setenv(rd.SKIP_REDACTION_ENV, raw)
        assert rd.resolve_skip_redaction(None) is False

    def test_explicit_redact_beats_the_env(self, monkeypatch) -> None:
        """An evaluation run must be able to force redaction back on inside a
        shell that exports the dev default."""
        monkeypatch.setenv(rd.SKIP_REDACTION_ENV, "1")
        assert rd.resolve_skip_redaction(False) is False

    def test_explicit_skip_beats_an_env_that_says_no(self, monkeypatch) -> None:
        monkeypatch.setenv(rd.SKIP_REDACTION_ENV, "0")
        assert rd.resolve_skip_redaction(True) is True


class TestSafetyIsNotRelaxed:
    """The switch removes a model call, not a check."""

    def test_a_clean_page_passes_through_unchanged(self) -> None:
        text = "보통약관 제1조(보상하는 손해)\n회사는 다음의 손해를 보상합니다."
        outcome = DevNoLlmRedactor().redact_page(text)
        assert outcome.redacted_text == text
        assert outcome.items_redacted == 0

    @pytest.mark.parametrize("text", [
        "환자 주민등록번호 850101-1234567 입니다",
        "연락처 010-1234-5678 로 회신 바랍니다",
    ])
    def test_structured_pii_still_blocks_the_page(self, text: str) -> None:
        """The same deterministic sweep LlmRedactor runs on its own output.

        If it ever stops raising here, the switch has become a hole rather
        than a cost saving.
        """
        with pytest.raises(RedactionLeakError):
            DevNoLlmRedactor().redact_page(text)

    def test_the_page_records_what_was_not_checked(self) -> None:
        """Unstructured PII (a bare personal name) is genuinely given up, so
        the outcome has to say so rather than look like a clean redaction."""
        outcome = DevNoLlmRedactor().redact_page("보통약관 제1조")
        assert outcome.review_warnings, "the give-up must be recorded"
        assert "unstructured" in outcome.review_warnings[0]

    def test_the_method_label_identifies_the_run(self) -> None:
        """`method` lands in redaction_result_{doc}.json, so a run that used
        the switch is identifiable afterwards and cannot be mistaken for a
        privacy-preserving one."""
        assert DevNoLlmRedactor.method == "dev_no_llm_redaction"
        assert DevNoLlmRedactor().label.startswith("dev_no_llm_redaction")


class TestSelector:
    def test_the_switch_short_circuits_before_the_manifest_read(self, monkeypatch) -> None:
        """Deliberate ordering, and the reason the switch survives what broke
        the class exemption: it must not depend on a `document_type`, which on
        the current stage order does not exist yet when redaction runs.
        """
        def explode(*a, **kw):
            raise AssertionError("manifest must not be read when skipping")

        monkeypatch.setattr(rd.dao, "read_contract_data", explode)
        redactor = rd._redactor_for("CASE_900", "DOC_001", "claude-cli", None,
                                    skip_redaction=True)
        assert isinstance(redactor, DevNoLlmRedactor)

    def test_without_the_switch_the_class_exemption_still_applies(self, monkeypatch) -> None:
        monkeypatch.delenv(rd.SKIP_REDACTION_ENV, raising=False)
        monkeypatch.setattr(rd.dao, "read_contract_data", lambda *a, **kw: {
            "documents": [{"document_id": "DOC_001",
                           "document_type": "insurance_policy"}]})
        redactor = rd._redactor_for("CASE_900", "DOC_001", "claude-cli", None)
        assert type(redactor).__name__ == "NoPiiClassRedactor"
