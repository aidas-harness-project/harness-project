"""Shared fixtures. Every dao.py filesystem test runs against a tmp_path,
never the real outputs/ or data/ -- see isolated_dao below.

schemas/ is NOT faked -- tests validate against the project's real schema
files, since that's the actual contract being tested.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import pytest

import dao


@pytest.fixture
def isolated_dao(tmp_path, monkeypatch):
    """Points dao.py's module-level OUTPUTS/DATA at a tmp dir for this test.

    dao's cmd_* functions read these as globals at call time (not bound at
    import time), so monkeypatching the module attributes redirects every
    case_dir()/processed_dir() call without touching the real project tree.
    """
    monkeypatch.setattr(dao, "OUTPUTS", tmp_path / "outputs")
    monkeypatch.setattr(dao, "DATA", tmp_path / "data")
    return tmp_path


@pytest.fixture
def case_id():
    return "CASE_009"


@pytest.fixture
def run_id():
    return "RUN_20260712_001"


@pytest.fixture
def make_args():
    """Builds an argparse.Namespace-like object for calling dao's cmd_*
    functions directly, without shelling out. Pass only the overrides a
    given test cares about; everything else defaults to None so a cmd_*
    function that ignores an unused attribute doesn't need it stubbed.
    """
    from types import SimpleNamespace

    def _make(**overrides):
        defaults = dict(
            case_id="CASE_009", doc_id="DOC_001", run_id="RUN_20260712_001",
            held_by="test-agent", purpose=None, stage=None,
            filename=None, data_file=None, schema_name=None,
            page=None, text_file=None, file_name=None, status=None,
            reviewer=None, reason=None, doc_path=None,
            topic=None, sources_file=None, conflict_id=None, verdict=None, note=None,
            caller_stage=None, description=None, version=None, fields_file=None,
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    return _make
