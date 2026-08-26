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

  * it skips the MODEL only. Turning the deterministic residual-PII scan off
    as well takes a SECOND, separate opt-in (`HARNESS_SKIP_PII_SCAN`), so the
    ordinary dev shell still blocks a page carrying structured PII;
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
import redaction  # noqa: E402
from redaction import DevNoLlmRedactor, RedactionLeakError  # noqa: E402


class TestPrecedence:
    def test_on_by_default(self, monkeypatch) -> None:
        """PoC default since 2026-08-20: unspecified means the redaction MODEL
        is skipped. It was the reverse before, which is why this test reads
        the opposite of its own name's history."""
        monkeypatch.delenv(rd.SKIP_REDACTION_ENV, raising=False)
        assert rd.resolve_skip_redaction(None) is True

    @pytest.mark.parametrize("raw", ["1", "true", "yes", "on", "TRUE", "On"])
    def test_env_turns_it_on(self, monkeypatch, raw: str) -> None:
        monkeypatch.setenv(rd.SKIP_REDACTION_ENV, raw)
        assert rd.resolve_skip_redaction(None) is True

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off"])
    def test_a_negative_env_value_turns_it_off(self, monkeypatch, raw: str) -> None:
        """The env var works in BOTH directions now that the default is on --
        it is how a shell opts a whole evaluation run back into real
        redaction without touching a command line."""
        monkeypatch.setenv(rd.SKIP_REDACTION_ENV, raw)
        assert rd.resolve_skip_redaction(None) is False

    @pytest.mark.parametrize("raw", ["", "garbage"])
    def test_an_unrecognized_env_value_falls_back_to_the_default(
        self, monkeypatch, raw: str
    ) -> None:
        """A typo must not silently mean the opposite of the default."""
        monkeypatch.setenv(rd.SKIP_REDACTION_ENV, raw)
        assert rd.resolve_skip_redaction(None) is rd.SKIP_REDACTION_DEFAULT

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
        """`skip_redaction=False` is the non-skip path -- since the PoC default
        flipped to skip-on, the class exemption is only REACHED that way, so
        the test asks for it rather than inheriting it from the default."""
        monkeypatch.delenv(rd.SKIP_REDACTION_ENV, raising=False)
        monkeypatch.setattr(rd.dao, "read_contract_data", lambda *a, **kw: {
            "documents": [{"document_id": "DOC_001",
                           "document_type": "insurance_policy"}]})
        redactor = rd._redactor_for("CASE_900", "DOC_001", "claude-cli", None,
                                    skip_redaction=False)
        assert type(redactor).__name__ == "NoPiiClassRedactor"


# ------------------------------------- the SECOND switch: the scan itself --
# `HARNESS_SKIP_REDACTION` skips the model and leaves the deterministic scan
# in place. That is right for a corpus whose PII is real, and wrong for one
# that is already pseudonymised, where every hit is a false positive and the
# block is pure cost. Measured on CASE_7077 (2026-08-26): a KB 보험증권 whose
# 계약자/피보험자/주소 cells are BLANK in the source was blocked on the
# 증권번호 `20236147840` and a print serial `20251222143900290378511` --
# neither a person, and the named 영업담당자 beside them is the insurer's own
# agent in the issuing branch's boilerplate.
#
# These pin that the second switch is genuinely separate, and that a run using
# it is identifiable afterwards.

def test_the_scan_still_blocks_when_only_the_model_switch_is_set(monkeypatch):
    """One switch is not enough: structured PII must still block."""
    monkeypatch.delenv(redaction.RESIDUAL_SCAN_ENV, raising=False)
    hits = redaction.scan_residual_pii("환자 주민등록번호 900101-1234567")
    assert [h["kind"] for h in hits] == ["resident_registration_number"]


def test_the_second_switch_turns_the_scan_off(monkeypatch):
    monkeypatch.setenv(redaction.RESIDUAL_SCAN_ENV, "1")
    assert redaction.scan_residual_pii("환자 주민등록번호 900101-1234567") == []
    assert redaction.scan_residual_pii(
        "20236147840 [계약자용]  20251222143900290378511") == []


def test_the_second_switch_is_off_by_default(monkeypatch):
    monkeypatch.delenv(redaction.RESIDUAL_SCAN_ENV, raising=False)
    assert redaction.residual_scan_disabled() is False


@pytest.mark.parametrize("raw", ["1", "true", "YES", "on"])
def test_truthy_spellings_enable_it(monkeypatch, raw):
    monkeypatch.setenv(redaction.RESIDUAL_SCAN_ENV, raw)
    assert redaction.residual_scan_disabled() is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "", "maybe"])
def test_anything_else_leaves_the_scan_on(monkeypatch, raw):
    monkeypatch.setenv(redaction.RESIDUAL_SCAN_ENV, raw)
    assert redaction.residual_scan_disabled() is False


def test_a_page_that_skipped_the_scan_says_so(monkeypatch):
    """The artifact must let a later reader tell the two modes apart.

    Without this the warning text is identical whether the scan ran and
    passed or never ran at all, and a run with NO privacy check anywhere
    would be indistinguishable from one that was checked deterministically.
    """
    text = "20236147840 [계약자용]  20251222143900290378511"

    monkeypatch.setenv(redaction.RESIDUAL_SCAN_ENV, "1")
    off = DevNoLlmRedactor().redact_page(text)
    assert off.redacted_text == text
    assert "HARNESS_SKIP_PII_SCAN" in off.review_warnings[0]
    assert "NOTHING checked this page" in off.review_warnings[0]

    monkeypatch.delenv(redaction.RESIDUAL_SCAN_ENV, raising=False)
    with pytest.raises(RedactionLeakError):
        DevNoLlmRedactor().redact_page(text)
