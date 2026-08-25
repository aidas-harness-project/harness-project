"""The sibling drivers refuse before the first paid call, like run_stage2 does.

`run_stage2` gained a phase-0 preflight, but `run_document_stage.py` and
`run_checkpoint1.py` are both invoked directly -- document-pipeline.md names
them -- and had none. The failure they avoid is not "the run fails": it is the
run failing HALFWAY, at redaction, after every page has already been OCR'd.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import llm_providers  # noqa: E402
import run_checkpoint1  # noqa: E402
import run_document_stage  # noqa: E402

API_ONLY = {"OPENROUTER_API_KEY": "test-key-not-a-real-credential"}


def args_for(checkpoint, **over):
    base = dict(
        case_id="CASE_999", checkpoint=checkpoint, skip_redaction=None,
        provider=None, model=None,
        reader_a=None, reader_b=None, comparator=None,
        reader_a_model=None, reader_b_model=None, comparator_model=None,
        classifier_provider=None, classifier_model=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


# ------------------------------------------------- the shared build check --

def test_preflight_reports_each_unbuildable_role_and_builds_nothing_else():
    failures = llm_providers.preflight(
        [("reader", "openrouter", None), ("redaction", "openrouter", "vendor/slug")],
        env=API_ONLY)

    assert len(failures) == 1
    assert failures[0].startswith("reader: ")


def test_preflight_does_not_swallow_an_execution_failure():
    # Only "cannot be configured" is a preflight matter. Anything else must
    # surface where it happens rather than being reported as a config problem.
    def explode(*a, **k):
        raise llm_providers.ProviderExecutionError("boom")

    original = llm_providers.build_provider
    llm_providers.build_provider = explode
    try:
        with pytest.raises(llm_providers.ProviderExecutionError):
            llm_providers.preflight([("x", "openrouter", "vendor/slug")], env=API_ONLY)
    finally:
        llm_providers.build_provider = original


# --------------------------------------------------- run_document_stage.py --

def test_checkpoint_1_blocks_when_a_reader_cannot_be_built(monkeypatch):
    monkeypatch.setattr(run_document_stage.os, "environ", dict(API_ONLY))
    failures = run_document_stage._preflight_for(args_for("1"))
    assert failures and "readers/comparator" in failures[0]


def test_checkpoint_1_does_not_demand_the_redaction_model(monkeypatch):
    # Checkpoint 1 never opens it; requiring it would refuse a valid OCR run.
    monkeypatch.setattr(run_document_stage.os, "environ",
                        {**API_ONLY, "HARNESS_OCR_READER_A_MODEL": "vendor/slug"})
    assert run_document_stage._preflight_for(args_for("1")) == []


def test_checkpoint_2_blocks_on_the_redaction_model(monkeypatch):
    monkeypatch.setattr(run_document_stage.os, "environ",
                        {**API_ONLY, "HARNESS_OCR_READER_A_MODEL": "vendor/slug"})
    failures = run_document_stage._preflight_for(args_for("2"))
    assert failures and "redaction" in failures[0]


def test_skipping_redaction_asks_for_no_redaction_provider(monkeypatch):
    monkeypatch.setattr(run_document_stage.os, "environ", dict(API_ONLY))
    assert run_document_stage._preflight_for(
        args_for("2", skip_redaction=True)) == []


def test_classify_without_an_explicit_classifier_stays_lazy(monkeypatch):
    # A printed form title settles most document types with no model call at
    # all. Demanding credentials up front would break that path.
    monkeypatch.setattr(run_document_stage.os, "environ", dict(API_ONLY))
    assert run_document_stage._preflight_for(args_for("classify")) == []


def test_classify_with_an_explicit_classifier_is_checked(monkeypatch):
    monkeypatch.setattr(run_document_stage.os, "environ", dict(API_ONLY))
    failures = run_document_stage._preflight_for(
        args_for("classify", classifier_provider="openrouter"))
    assert failures and "classification" in failures[0]


def test_a_resolvable_selection_passes_every_checkpoint(monkeypatch):
    monkeypatch.setattr(run_document_stage.os, "environ",
                        {**API_ONLY, "OPENROUTER_MODEL": "vendor/slug"})
    for checkpoint in ("1", "2", "classify"):
        assert run_document_stage._preflight_for(args_for(checkpoint)) == []


def test_the_driver_exits_without_running_anything(monkeypatch, capsys):
    import json
    monkeypatch.setattr(run_document_stage.os, "environ", dict(API_ONLY))
    ran = []
    monkeypatch.setattr(run_document_stage, "run_ocr_stage",
                        lambda *a, **k: ran.append("ocr") or {"status": "success"},
                        raising=False)

    code = run_document_stage.main(
        ["CASE_999", "--held-by", "tester", "--run-id", "RUN_1"])

    assert code == 1
    assert ran == [], "nothing may run once preflight has failed"
    assert json.loads(capsys.readouterr().out)["stopped_at"] == "preflight"


# ------------------------------------------------------- run_checkpoint1.py --

def test_the_single_document_run_refuses_before_reading_a_pdf(monkeypatch, capsys):
    monkeypatch.setattr(run_checkpoint1.os, "environ", dict(API_ONLY))

    with pytest.raises(SystemExit) as excinfo:
        run_checkpoint1.main(["run", "CASE_999", "DOC_001", "missing.pdf",
                              "--held-by", "tester", "--run-id", "RUN_1"])

    assert "readers/comparator cannot be built" in str(excinfo.value)


def test_classify_only_still_builds_its_provider_lazily(monkeypatch):
    # The deliberate exception: classify-only may complete with no model call,
    # so it must not be gated on credentials for a call that never happens.
    monkeypatch.setattr(run_checkpoint1.os, "environ", dict(API_ONLY))
    seen = []
    monkeypatch.setattr(run_checkpoint1, "classify_existing",
                        lambda *a, **k: seen.append(a) or {"status": "success"})

    run_checkpoint1.main(["classify-only", "CASE_999", "DOC_001",
                          "--held-by", "tester", "--run-id", "RUN_1"])

    assert seen, "classify-only must still run without a resolvable provider"
