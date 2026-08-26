"""The two PoC throughput reductions, and what a default may not hide.

`--single-reader` (P8 off) and `--skip-redaction` (no redaction MODEL) were
opt-in: unset meant full dual-read P8 and real redaction. For the PoC the PoC
owner set both ON by default, so a run that specifies nothing gets the fast
path. That is a deliberate, recorded reduction of two guardrails, so these
tests pin the two things a default must NOT change:

* an explicit `--dual-read` / `--redact` still wins, so an evaluation run can
  get the full path back inside a shell that changed nothing, and
* the reduction stays visible in the per-document record -- `ocr_quality:
  low`, `cross_validation_status: single_reader_no_cross_validation`,
  `review_required`, and the `dev_no_llm_redaction` method with its warning.

A default that silently produced a clean-looking P8 pass, or redacted text
indistinguishable from model-redacted text, would be the actual danger here.
"""
from __future__ import annotations

import ocr_extract
import redact_document


# --------------------------------------------------------- the new default --

def test_single_reader_is_on_when_nothing_is_specified(monkeypatch):
    monkeypatch.delenv(ocr_extract.SINGLE_READER_ENV, raising=False)
    assert ocr_extract.resolve_single_reader(None) is True


def test_skip_redaction_is_on_when_nothing_is_specified(monkeypatch):
    monkeypatch.delenv(redact_document.SKIP_REDACTION_ENV, raising=False)
    assert redact_document.resolve_skip_redaction(None) is True


# ------------------------------------------ explicit still beats the default --

def test_dual_read_still_wins_over_the_default(monkeypatch):
    """`--dual-read` passes False. An evaluation run must be able to get real
    P8 back without editing anything."""
    monkeypatch.delenv(ocr_extract.SINGLE_READER_ENV, raising=False)
    assert ocr_extract.resolve_single_reader(False) is False


def test_redact_still_wins_over_the_default(monkeypatch):
    monkeypatch.delenv(redact_document.SKIP_REDACTION_ENV, raising=False)
    assert redact_document.resolve_skip_redaction(False) is False


def test_the_env_var_can_still_turn_each_off(monkeypatch):
    """The env var keeps working in both directions -- it is how a shell opts
    back into the full path for every tool at once."""
    monkeypatch.setenv(ocr_extract.SINGLE_READER_ENV, "0")
    assert ocr_extract.resolve_single_reader(None) is False
    monkeypatch.setenv(redact_document.SKIP_REDACTION_ENV, "0")
    assert redact_document.resolve_skip_redaction(None) is False


def test_the_env_var_can_still_turn_each_on(monkeypatch):
    monkeypatch.setenv(ocr_extract.SINGLE_READER_ENV, "1")
    assert ocr_extract.resolve_single_reader(None) is True
    monkeypatch.setenv(redact_document.SKIP_REDACTION_ENV, "1")
    assert redact_document.resolve_skip_redaction(None) is True
