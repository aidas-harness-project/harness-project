"""One file says which model each stage runs on, and the pipeline reads it.

Before it, the answer was in three incomplete places: `model:` in eleven
`.claude/agents/*.md` frontmatters (a Claude Code tier, not a provider slug, and
only for stages that run as subagents), a scatter of per-role environment
variables, and whatever `--provider`/`--model` an operator typed. Nothing could
be read to answer the question for the whole pipeline.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import ocr_extract  # noqa: E402
import stage_models  # noqa: E402
from _validation import load_registry, validate_instance  # noqa: E402

CONFIG = {
    "schema_version": "stage_model_config.v0.1",
    "default": {"provider": "openrouter", "model": "vendor/default"},
    "stages": {
        "document_processing": {
            "model": "vendor/stage",
            "roles": {"reader_a": {"model": "vendor/a"},
                      "reader_b": {"provider": "claude-cli"}},
        },
        "critic_v1": {},
    },
}


# ------------------------------------------------------------- precedence --

@pytest.mark.parametrize("stage,role,expected", [
    ("document_processing", "reader_a", ("openrouter", "vendor/a")),
    # A role inherits the half it does not set -- here the stage's model.
    ("document_processing", "reader_b", ("claude-cli", "vendor/stage")),
    ("document_processing", None, ("openrouter", "vendor/stage")),
    # An entry that decides nothing falls through to `default`...
    ("critic_v1", None, ("openrouter", "vendor/default")),
    # ...and so does a stage with no entry at all.
    ("denial_validation", None, ("openrouter", "vendor/default")),
])
def test_role_then_stage_then_default(stage, role, expected):
    assert stage_models.resolve(stage, role, config=CONFIG) == expected


def test_an_explicit_flag_beats_the_file():
    # An operator saying it now beats a file saying it earlier.
    assert stage_models.resolve(
        "document_processing", "reader_a",
        provider="fixture", model="vendor/cli", config=CONFIG) == (
            "fixture", "vendor/cli")


def test_each_half_is_resolved_independently():
    # Naming a provider on the command line must not discard the model the file
    # records for it, or `--provider X` would silently unset the model.
    assert stage_models.resolve(
        "document_processing", "reader_a",
        provider="fixture", config=CONFIG) == ("fixture", "vendor/a")


def test_nothing_decided_resolves_to_nothing():
    # None, not a plausible slug: it lets the existing "openrouter requires a
    # model name" refusal fire at selection instead of being papered over.
    assert stage_models.resolve("critic_v1", config={"stages": {}}) == (None, None)


# ---------------------------------------------------------- the real file --

def test_the_shipped_config_validates():
    config = json.loads(stage_models.CONFIG_PATH.read_text(encoding="utf-8"))
    schemas, registry = load_registry()
    assert validate_instance(config, stage_models.SCHEMA_NAME, schemas, registry) == []


def test_the_shipped_config_decides_nothing_yet():
    # It ships EMPTY on purpose. A plausible slug written here would be
    # indistinguishable from a decision nobody made, and the pipeline must
    # behave exactly as it did before the file existed.
    for stage, entry in stage_models.configured_stages().items():
        assert entry["provider"] is None and entry["model"] is None, stage
        for role, selection in (entry.get("roles") or {}).items():
            assert selection == {"provider": None, "model": None}, f"{stage}.{role}"


def test_every_configured_stage_is_a_real_stage():
    # A stage this pipeline cannot open is a typo, not a configuration, and a
    # typo would silently never match.
    run_state = (ROOT / "schemas" / "run_state.schema.json").read_text(encoding="utf-8")
    known = set(re.findall(r'"([a-z_0-9]+)"', run_state))
    for stage in stage_models.configured_stages():
        assert stage in known, f"{stage} is not a stage run_state.schema.json knows"


def test_a_malformed_config_is_an_error_not_a_silent_default(tmp_path):
    # Falling back to "no configuration" would make a typo look like a
    # deliberate absence, and the two must stay distinguishable.
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    stage_models.load_config.cache_clear()
    try:
        with pytest.raises(stage_models.StageModelConfigError):
            stage_models.load_config(str(broken))
    finally:
        stage_models.load_config.cache_clear()


def test_a_missing_config_falls_through_to_the_environment(tmp_path):
    # A deployment without the file is legitimate: every lookup then behaves
    # as it did before the file existed.
    stage_models.load_config.cache_clear()
    try:
        assert stage_models.load_config(str(tmp_path / "absent.json")) == {}
    finally:
        stage_models.load_config.cache_clear()


# ----------------------------------------------------------- the consumers --

def test_the_p8_readers_actually_read_it(monkeypatch):
    """The roles are only worth declaring if something consumes them.

    Asserted on the providers `build_ocr_providers` returns, not on the
    resolver in isolation -- a config nothing reads is the exact smell
    known-gaps 57 records.
    """
    monkeypatch.setattr(stage_models, "load_config", lambda path=None: {
        "stages": {"document_processing": {
            "provider": "fixture",
            "roles": {"reader_a": {"model": "vendor/a"},
                      "reader_b": {"model": "vendor/b"},
                      "comparator": {"model": "vendor/c"}}}}})

    providers = ocr_extract.build_ocr_providers(env={})

    assert providers["reader_a"].model_name == "vendor/a"
    assert providers["reader_b"].model_name == "vendor/b"
    assert providers["comparator"].model_name == "vendor/c"


def test_an_explicit_reader_flag_still_wins_over_the_file(monkeypatch):
    monkeypatch.setattr(stage_models, "load_config", lambda path=None: {
        "stages": {"document_processing": {
            "provider": "fixture",
            "roles": {"reader_a": {"model": "vendor/a"}}}}})

    providers = ocr_extract.build_ocr_providers(
        reader_a_model="vendor/flag", env={})

    assert providers["reader_a"].model_name == "vendor/flag"


def test_the_file_ranks_above_the_environment(monkeypatch):
    """Checked in and reviewed beats ambient.

    BOTH halves are asserted against a competing environment variable. An
    earlier version of this test set only `HARNESS_OCR_READER_A_MODEL`, so
    reversing the PROVIDER precedence broke nothing and the test reported a
    guard it was not holding -- caught by reintroducing exactly that swap.
    """
    monkeypatch.setattr(stage_models, "load_config", lambda path=None: {
        "stages": {"document_processing": {
            "roles": {"reader_a": {"provider": "fixture",
                                   "model": "vendor/from-file"}}}}})

    providers = ocr_extract.build_ocr_providers(
        env={"HARNESS_OCR_READER_A_PROVIDER": "claude-cli",
             "HARNESS_OCR_READER_A_MODEL": "vendor/from-env"})

    assert providers["reader_a"].provider_name == "fixture"
    assert providers["reader_a"].model_name == "vendor/from-file"


def test_an_undecided_role_leaves_the_environment_in_charge(monkeypatch):
    monkeypatch.setattr(stage_models, "load_config", lambda path=None: {
        "stages": {"document_processing": {"roles": {"reader_a": {}}}}})

    providers = ocr_extract.build_ocr_providers(
        env={"HARNESS_OCR_READER_A_PROVIDER": "fixture",
             "HARNESS_OCR_READER_A_MODEL": "vendor/from-env"})

    assert providers["reader_a"].model_name == "vendor/from-env"


def test_the_cli_reports_what_runs_on_what():
    import subprocess
    out = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "stage_models.py")],
        capture_output=True, text=True, timeout=60).stdout
    reported = json.loads(out)
    assert "document_processing" in reported
    assert "reader_a" in reported["document_processing"]["roles"]


def test_redaction_leaves_room_for_the_file_to_apply():
    """Its argparse defaults must not bake the environment in.

    They used to: `--provider` defaulted to
    `os.environ.get("HARNESS_REDACTION_PROVIDER", DEFAULT_REDACTION_PROVIDER)`,
    so `args.provider` was non-None on every run and a recorded selection could
    never have reached the resolver. Asserted on the parser rather than on a
    full run, because that is exactly where the defect was.
    """
    import argparse
    import redact_document

    source = Path(redact_document.__file__).read_text(encoding="utf-8")
    assert 'parser.add_argument("--provider", choices=SUPPORTED_PROVIDERS, default=None)' in source
    assert 'parser.add_argument("--model", default=None)' in source
    assert 'stage_models.resolve(\n        "document_processing", "redaction"' in source
