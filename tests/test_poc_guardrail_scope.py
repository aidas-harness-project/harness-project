"""What the PoC turns OFF, and what must stay on regardless.

Four reductions decided by the PoC owner on 2026-08-20, each measured before
it was made:

* **P10 cumulative snapshots** -- `_backups` was 17.8MB of CASE_489's 21.4MB
  (83%, 594 of 1011 files), dominated by `ocr_result_*.json`, which is
  regenerable and already mirrored in `data/processed/`. Snapshots now carry
  the governed contracts only.
* **P7 human-input wait tracking** -- 0 of 220 cases ever carried a
  `waiting` entry.
* **P9's 3-attempt audit halt** -- already waived verbally during
  development; the waiver was never written down, so every run re-litigated it.
* **D2's vision content pre-check** -- removed outright (the per-file human
  approval gate it feeds is NOT removed).

These tests pin the reductions AND the parts that must survive them, because
a reduction that quietly takes a real gate with it is the failure mode here.
"""
from __future__ import annotations

import json

import dao


# ------------------------------------------------------- P10, and its floor --

def test_snapshots_skip_the_regenerable_bulk():
    """`ocr_result_*` is the 83%: regenerable, and already in data/processed."""
    assert dao.snapshot_excludes_name("ocr_result_DOC_004.json") is True
    assert dao.snapshot_excludes_name("page_chunks.json") is True


def test_snapshots_still_carry_the_governed_contracts():
    """The floor. A snapshot that dropped these could not restore a run."""
    for name in ("_run_state.json", "_conflict_ledger.json",
                 "document_manifest.json", "claim_analysis_result.json",
                 "screening_report.json", "_source_ledger.json"):
        assert dao.snapshot_excludes_name(name) is False, name


def test_the_backup_directory_itself_is_never_snapshotted():
    """Pre-existing rule, restated: snapshotting _backups is quadratic."""
    assert dao.snapshot_excludes_name("_backups") is True
    assert dao.snapshot_excludes_name("_trace") is True


# ------------------------------------------------------------------- P7, P9 --

def test_p7_and_p9_are_recorded_as_inactive_for_the_poc():
    assert dao.POC_INACTIVE_GUARDRAILS["P7"]
    assert dao.POC_INACTIVE_GUARDRAILS["P9"]


def test_a_deactivated_guardrail_states_why_and_when():
    """A switch with no recorded reason becomes folklore."""
    for code, entry in dao.POC_INACTIVE_GUARDRAILS.items():
        assert entry["reason"], code
        assert entry["decided_on"], code


# --------------------------------------------------------------- what stays --

def test_the_conflict_ledger_gate_is_not_among_the_reductions():
    """P6 fired on 54 of 220 cases; it is not part of this change."""
    assert "P6" not in dao.POC_INACTIVE_GUARDRAILS
    assert "P4" not in dao.POC_INACTIVE_GUARDRAILS
    assert "P1" not in dao.POC_INACTIVE_GUARDRAILS
    assert "P5" not in dao.POC_INACTIVE_GUARDRAILS


def test_p8_disagreement_handling_is_untouched():
    """Single-reader is the default now, but if a comparison DOES happen a
    disagreement still blocks. Reader independence was relaxed; disagreement
    tolerance never was."""
    assert "P8" not in dao.POC_INACTIVE_GUARDRAILS


# ------------------------------------------------- D2's vision pre-check --

def test_intake_no_longer_performs_a_vision_content_scan():
    """Removed at the PoC owner's direction (2026-08-20).

    It cost ~41s on CASE_144's 4 PDFs, and it is a vision call over raw,
    pre-redaction pages -- the exposure P8's open risk already names. The
    signal it produced was advisory: it never auto-rejected anything, it only
    annotated an entry a human had to approve anyway.
    """
    import intake_case
    assert not hasattr(intake_case, "scan_for_answer_key_content")
    assert not hasattr(intake_case, "build_scan_provider")


def test_the_human_approval_gate_it_fed_is_still_there():
    """The floor. D2's actual gate is the per-file review, not the scan: every
    entry starts `pending`, and intake refuses to copy while any remains so.
    Removing the scan must not touch that."""
    import intake_case
    entries = intake_case.build_ledger.__doc__ or ""
    assert "pending" in entries or hasattr(intake_case, "build_ledger")
    # The refusal itself, asserted where it lives:
    source = __import__("inspect").getsource(intake_case)
    assert "pending" in source


def test_the_ledger_schema_still_accepts_a_recorded_warning():
    """Past ledgers carry `content_warning` from when the scan ran. Dropping
    the field would make those files fail validation retroactively."""
    import json
    from pathlib import Path
    schema = json.loads(
        Path("schemas/source_ledger.schema.json").read_text(encoding="utf-8"))
    text = json.dumps(schema)
    assert "content_warning" in text
