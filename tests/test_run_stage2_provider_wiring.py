"""Stage 2 forwards BOTH halves of a provider selection, and checks them first.

`run_stage2.py` forwarded `--provider` to every child and had no `--model` flag
at all. That was invisible while every selectable default was a CLI needing no
model (`claude-cli` supplies its own sentinel); the moment the default became an
HTTP provider with a mandatory slug, "the way Stage 2 is run" could not name a
model on the command line.

The second half matters as much as the first: each role resolves its model from
a DIFFERENT env var, so a partial environment satisfied checkpoint 1 and failed
at redaction -- after the whole case had been OCR'd and paid for.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import run_stage2  # noqa: E402


API_ONLY = {"OPENROUTER_API_KEY": "test-key-not-a-real-credential"}


# ------------------------------------------------------------- preflight --

def test_a_provider_that_needs_a_model_blocks_before_any_work():
    failures = run_stage2._preflight_providers(
        "openrouter", None, skip_redaction=None, env=API_ONLY)

    # Every role, named, so the operator fixes them in one pass rather than one
    # failed run at a time.
    assert len(failures) == 3
    assert all("requires a model name" in f for f in failures)
    assert [f.split(":")[0] for f in failures] == [
        "checkpoint 1 readers/comparator",
        "classifier / segmentation judge",
        "checkpoint 2 redaction",
    ]


def test_the_partial_environment_that_used_to_die_at_redaction_is_caught_up_front():
    # The exact hazard: HARNESS_OCR_READER_A_MODEL satisfies the OCR trio and
    # nothing else, so checkpoint 1 ran, the document was OCR'd, and redaction
    # then refused to build. Now the stage never starts.
    failures = run_stage2._preflight_providers(
        "openrouter", None, skip_redaction=None,
        env={**API_ONLY, "HARNESS_OCR_READER_A_MODEL": "vendor/slug"})

    roles = [f.split(":")[0] for f in failures]
    assert "checkpoint 1 readers/comparator" not in roles, (
        "the OCR trio resolves -- that is precisely why this used to get past "
        "checkpoint 1")
    assert "checkpoint 2 redaction" in roles


@pytest.mark.parametrize("provider,model,env", [
    ("openrouter", "vendor/slug", API_ONLY),
    ("openrouter", None, {**API_ONLY, "OPENROUTER_MODEL": "vendor/slug"}),
    ("openrouter", None, {**API_ONLY, "HARNESS_OPENROUTER_MODEL": "vendor/slug"}),
    # A CLI provider still needs no model at all -- the flag did not become
    # mandatory for the transports that never wanted it.
    ("claude-cli", None, {}),
    (None, None, {**API_ONLY, "OPENROUTER_MODEL": "vendor/slug"}),
])
def test_a_resolvable_selection_passes_preflight(provider, model, env):
    assert run_stage2._preflight_providers(
        provider, model, skip_redaction=None, env=env) == []


def test_skipping_redaction_does_not_demand_a_redaction_provider():
    failures = run_stage2._preflight_providers(
        "openrouter", None, skip_redaction=True,
        env={**API_ONLY, "HARNESS_OCR_READER_A_MODEL": "vendor/slug",
             "HARNESS_LLM_MODEL": "vendor/slug"})
    assert failures == []


def test_preflight_runs_before_the_first_child_and_stops_the_stage(monkeypatch):
    calls = []
    monkeypatch.setattr(run_stage2, "_run",
                        lambda argv, **kw: calls.append(argv) or {"returncode": 0})
    monkeypatch.setattr(run_stage2.os, "environ", dict(API_ONLY))

    result = run_stage2.run_stage2("CASE_999", "tester", "RUN_1",
                                   provider="openrouter")

    assert result["status"] == "failed"
    assert result["stopped_at"] == "preflight"
    assert calls == [], "nothing may be executed once preflight has failed"


# -------------------------------------------------------- model forwarding --

@pytest.fixture
def captured(monkeypatch):
    """Run the stage with every child stubbed, recording the argv it built.

    The run deliberately ends at chunking (no chunkable document): phases 6 and
    7 write real files, and every provider-backed argv has already been built
    by then.
    """
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return {"phase": kwargs.get("phase"), "returncode": 0}

    monkeypatch.setattr(run_stage2, "_run", fake_run)
    monkeypatch.setattr(run_stage2, "_manifest", lambda case_id: {})
    monkeypatch.setattr(run_stage2, "pending_bundles", lambda manifest: [])
    monkeypatch.setattr(run_stage2, "unclassified_children", lambda manifest: [])
    monkeypatch.setattr(run_stage2, "chunkable_documents", lambda manifest: ([], []))
    monkeypatch.setattr(run_stage2, "_preflight_providers",
                        lambda *a, **k: [])
    return calls


def argv_for(calls, tool):
    return next(a for a in calls if any(str(a_i).endswith(tool) for a_i in a))


def test_the_model_reaches_every_checkpoint_1_role(captured):
    run_stage2.run_stage2("CASE_999", "tester", "RUN_1",
                          provider="openrouter", model="vendor/slug")

    argv = argv_for(captured, "run_document_stage.py")
    for flag in ("--reader-a-model", "--reader-b-model",
                 "--comparator-model", "--classifier-model"):
        assert flag in argv, f"{flag} not forwarded"
        assert argv[argv.index(flag) + 1] == "vendor/slug"
    # And the provider half is still forwarded alongside it.
    assert argv[argv.index("--reader-a") + 1] == "openrouter"


def test_the_model_reaches_redaction(captured):
    run_stage2.run_stage2("CASE_999", "tester", "RUN_1",
                          provider="openrouter", model="vendor/slug")

    redaction = [a for a in captured if "--checkpoint" in a]
    assert redaction, "no redaction step was built"
    for argv in redaction:
        assert argv[argv.index("--model") + 1] == "vendor/slug"


def test_the_model_reaches_the_segmentation_proposal(captured, monkeypatch):
    monkeypatch.setattr(run_stage2, "pending_bundles",
                        lambda manifest: [{"document_id": "DOC_001"}])
    monkeypatch.setattr(run_stage2, "_proposal", lambda case_id, doc_id: None)

    run_stage2.run_stage2("CASE_999", "tester", "RUN_1",
                          provider="openrouter", model="vendor/slug")

    argv = argv_for(captured, "segment_case.py")
    assert argv[argv.index("--model") + 1] == "vendor/slug"


def test_the_model_reaches_split_child_classification(captured, monkeypatch):
    monkeypatch.setattr(run_stage2, "unclassified_children",
                        lambda manifest: [{"document_id": "DOC_002"}])

    run_stage2.run_stage2("CASE_999", "tester", "RUN_1",
                          provider="openrouter", model="vendor/slug")

    argv = argv_for(captured, "run_checkpoint1.py")
    assert "classify-only" in argv
    assert argv[argv.index("--classifier-model") + 1] == "vendor/slug"
    assert argv[argv.index("--classifier-provider") + 1] == "openrouter"


def test_no_model_flag_appears_when_none_was_selected(captured):
    # A CLI provider run must look exactly as it did before: passing an empty
    # --model would turn "use your own default" into "use the model named ''".
    run_stage2.run_stage2("CASE_999", "tester", "RUN_1", provider="claude-cli")

    for argv in captured:
        assert "--model" not in argv
        assert "--reader-a-model" not in argv
        assert "--classifier-model" not in argv
