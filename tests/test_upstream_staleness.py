"""Upstream rewrites must not leave stale downstream contracts looking valid.

`denial_validation_result.json` and `screening_report.json` are both derived
from `denial_reason_result.json`. Nothing tied them to the version they were
built from, so rewriting the upstream contract -- adding a reason, flipping a
decision_type, dropping a policy match -- left the downstream files on disk
describing a reason set that no longer existed, with run-state still reading
`passed`. Every id in them might still resolve against the NEW contract by
coincidence, so the cross-contract id checks could not catch it either.

The mechanism: a content hash of what downstream actually resolves against
(reason ids, decision types, taxonomy codes, owned policy_match_ids -- not the
whole file, so cosmetic edits do not invalidate correct work). Downstream
records it; the DAO recomputes it from disk at write time and refuses a
mismatch. Rewriting upstream flips affected stages back to `pending` and
leaves the files alone.

TOCTOU: the check runs inside the downstream file's own lock, and reads
upstream at that moment, so there is no window between verifying and writing.
Lock order is one-directional -- downstream file, then (separately) run-state
-- so no two callers can hold locks in opposing order.
"""
import copy
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import dao
import _cross_contract as cc

ROOT = Path(__file__).resolve().parent.parent
REAL_DENIAL = ROOT / "outputs" / "CASE_903" / "denial_reason_result.json"


@pytest.fixture
def real_reasons():
    """The real CASE_903 contract -- schema-valid and cross-contract-clean, so
    a test that fails fails on staleness rather than on fixture drift."""
    if not REAL_DENIAL.exists():
        pytest.skip("CASE_903 output not present in this checkout")
    data = json.loads(REAL_DENIAL.read_text(encoding="utf-8"))
    data["case_id"] = "CASE_009"
    return data


@pytest.fixture
def seeded_case(isolated_dao, real_reasons):
    case = dao.case_dir("CASE_009")
    case.mkdir(parents=True, exist_ok=True)
    (case / "denial_reason_result.json").write_text(
        json.dumps(real_reasons, ensure_ascii=False), encoding="utf-8")
    return case


def _screening(hash_value, denial_ids=("DR_1",)):
    return {
        "source_denial_contract_hash": hash_value,
        "insurer_position": {
            "denial": {"reason_ids": list(denial_ids)},
            "reduction": {"reason_ids": []},
        },
    }


def _run_state(stage, status):
    return {
        "case_id": "CASE_009", "run_id": "RUN_20260712_001",
        "created_at": dao.now_iso(), "updated_at": dao.now_iso(),
        "stages": [{"stage_name": stage, "status": status, "started_at": None,
                    "completed_at": None, "attempt_count": 1, "backup_path": None}],
        "human_input_status": [],
    }


def _meaningfully_changed(reasons):
    """A change downstream would actually care about: a reason's decision_type
    and code, not its prose."""
    changed = copy.deepcopy(reasons)
    first = changed["denial_reasons"][0]
    first["decision_type"] = "reduction"
    first["taxonomy_code"] = "R01"
    first["taxonomy_label"] = "기왕증 / 기존 질환 기여도"
    first["candidate_codes"] = [{"taxonomy_code": "R01", "confidence": 0.9}]
    return changed


def _write_args(tmp_path, payload, filename, schema, **over):
    f = tmp_path / f"payload_{filename}"
    f.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    base = dict(case_id="CASE_009", filename=filename, data_file=str(f),
                schema_name=schema, held_by="test-agent",
                run_id="RUN_20260712_001", purpose=None, stage=None)
    base.update(over)
    return SimpleNamespace(**base)


# ---- what the hash is, and is not, sensitive to ----

def test_hash_ignores_cosmetic_edits(real_reasons):
    """An invalidation that fires on noise gets ignored, which is worse than
    none -- so rewording a summary must not strand correct downstream work."""
    before = cc.upstream_hash(real_reasons)
    reworded = copy.deepcopy(real_reasons)
    reworded["denial_reasons"][0]["insurer_claim_summary"] = "같은 뜻, 다른 문장"
    reworded["warnings"] = ["a new warning"]
    assert cc.upstream_hash(reworded) == before


@pytest.mark.parametrize("mutate,label", [
    (lambda d: d["denial_reasons"][0].update(decision_type="reduction"), "decision_type"),
    (lambda d: d["denial_reasons"][0].update(taxonomy_code="R01"), "taxonomy_code"),
    (lambda d: d["denial_reasons"].pop(), "a dropped reason"),
    (lambda d: d["denial_reasons"][0]["policy_matches"].append({"policy_match_id": "PM_9"}),
     "a new policy match"),
])
def test_hash_changes_on_material_edits(real_reasons, mutate, label):
    before = cc.upstream_hash(real_reasons)
    mutate(real_reasons)
    assert cc.upstream_hash(real_reasons) != before, f"{label} must invalidate downstream"


def test_hash_is_order_independent(real_reasons):
    """Reordering the array is not a semantic change; downstream resolves by
    id, not position."""
    before = cc.upstream_hash(real_reasons)
    real_reasons["denial_reasons"].reverse()
    assert cc.upstream_hash(real_reasons) == before


# ---- downstream writes are refused when built against a stale upstream ----

def test_downstream_built_on_current_upstream_is_accepted(seeded_case, real_reasons):
    doc = _screening(cc.upstream_hash(real_reasons))
    assert cc.check("screening_report.json", doc, seeded_case) == []


def test_downstream_built_on_a_superseded_upstream_is_refused(seeded_case, real_reasons):
    """The regression: upstream changed after this report was derived."""
    stale_hash = cc.upstream_hash(_meaningfully_changed(real_reasons))
    errors = cc.check("screening_report.json", _screening(stale_hash), seeded_case)
    assert any("has since changed" in e for e in errors)


def test_downstream_without_a_recorded_hash_is_refused(seeded_case):
    """No recorded hash means nothing establishes which reason set this came
    from -- treated as stale rather than assumed current."""
    doc = _screening(None)
    doc.pop("source_denial_contract_hash")
    errors = cc.check("screening_report.json", doc, seeded_case)
    assert any("is missing" in e for e in errors)


def test_validation_contract_is_hash_checked_too(seeded_case, real_reasons):
    validations = [{"reason_id": r["reason_id"], "verdict": "partially_supported",
                    "policy_match_validations": []}
                   for r in real_reasons["denial_reasons"]]
    stale = {"source_denial_contract_hash": cc.upstream_hash(_meaningfully_changed(real_reasons)),
             "validations": validations}
    errors = cc.check("denial_validation_result.json", stale, seeded_case)
    assert any("has since changed" in e for e in errors)

    current = dict(stale, source_denial_contract_hash=cc.upstream_hash(real_reasons))
    assert cc.check("denial_validation_result.json", current, seeded_case) == []


# ---- rewriting upstream invalidates, but never deletes ----

def test_upstream_rewrite_resets_stale_stage_and_keeps_the_file(seeded_case, real_reasons,
                                                               tmp_path, capsys):
    (seeded_case / "screening_report.json").write_text(
        json.dumps(_screening(cc.upstream_hash(real_reasons)), ensure_ascii=False),
        encoding="utf-8")
    (seeded_case / "_run_state.json").write_text(
        json.dumps(_run_state("screening_report", "passed"), ensure_ascii=False),
        encoding="utf-8")

    rc = dao.cmd_write_contract(_write_args(
        tmp_path, _meaningfully_changed(real_reasons),
        "denial_reason_result.json", "denial_reason_result.schema.json"))
    out = capsys.readouterr().out

    assert rc == 0, "the upstream write itself is legitimate and must succeed"
    state = json.loads((seeded_case / "_run_state.json").read_text(encoding="utf-8"))
    stage = next(s for s in state["stages"] if s["stage_name"] == "screening_report")
    assert stage["status"] == "pending", "a stale downstream stage must not still read passed"
    assert (seeded_case / "screening_report.json").exists(), \
        "the stale contract is evidence of what a stage concluded -- never deleted (P6)"
    assert "no longer describe the current reason/match set" in out


def test_cosmetic_upstream_rewrite_leaves_downstream_alone(seeded_case, real_reasons,
                                                          tmp_path):
    """Correct downstream work must survive an unrelated upstream edit."""
    (seeded_case / "screening_report.json").write_text(
        json.dumps(_screening(cc.upstream_hash(real_reasons)), ensure_ascii=False),
        encoding="utf-8")
    (seeded_case / "_run_state.json").write_text(
        json.dumps(_run_state("screening_report", "passed"), ensure_ascii=False),
        encoding="utf-8")

    reworded = copy.deepcopy(real_reasons)
    reworded["denial_reasons"][0]["insurer_claim_summary"] = "같은 뜻, 다른 문장"

    assert dao.cmd_write_contract(_write_args(
        tmp_path, reworded, "denial_reason_result.json",
        "denial_reason_result.schema.json")) == 0

    state = json.loads((seeded_case / "_run_state.json").read_text(encoding="utf-8"))
    stage = next(s for s in state["stages"] if s["stage_name"] == "screening_report")
    assert stage["status"] == "passed"


def test_upstream_rewrite_with_no_downstream_yet_is_silent(seeded_case, real_reasons, tmp_path,
                                                           capsys):
    """Phase 1 rewrites before Phase 2 exists must not emit invalidation noise."""
    assert dao.cmd_write_contract(_write_args(
        tmp_path, _meaningfully_changed(real_reasons), "denial_reason_result.json",
        "denial_reason_result.schema.json")) == 0
    assert "no longer describe" not in capsys.readouterr().out


# ---- concurrency: the check must not have a TOCTOU window ----

def test_downstream_write_waits_for_upstream_lock_and_sees_the_new_hash(
    seeded_case, real_reasons, tmp_path, monkeypatch
):
    """A downstream write racing an in-flight upstream rewrite.

    The downstream contract is built against the OLD upstream. While it is
    blocked on its own lock, upstream is rewritten. When the write finally
    proceeds it must read upstream as it now stands and refuse -- not act on
    the hash it computed before waiting.
    """
    monkeypatch.setattr(dao, "LOCK_POLL_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(dao, "LOCK_MAX_WAIT_SECONDS", 5.0)

    old_hash = cc.upstream_hash(real_reasons)
    target = seeded_case / "screening_report.json"
    dao.acquire_lock(target, "other-writer", "RUN_20260712_002", "holding briefly")

    # Written atomically, so a reader can only ever see the whole old file or
    # the whole new one. Otherwise this test could "pass" on a JSON parse
    # error from a half-written file rather than on the hash check -- which is
    # exactly what a first version of it did, hiding the real assertion.
    upstream = seeded_case / "denial_reason_result.json"
    rewritten = json.dumps(_meaningfully_changed(real_reasons), ensure_ascii=False)
    started = threading.Event()

    def rewrite_upstream_then_release():
        started.wait(2.0)
        time.sleep(0.05)
        scratch = upstream.with_suffix(".json.tmp")
        scratch.write_text(rewritten, encoding="utf-8")
        scratch.replace(upstream)
        dao.release_lock(target)

    worker = threading.Thread(target=rewrite_upstream_then_release)
    worker.start()
    started.set()

    rc = dao.cmd_write_contract(_write_args(
        tmp_path, _screening(old_hash), "screening_report.json",
        "screening_report.schema.json"))
    worker.join(5.0)

    assert rc == 1, "must refuse: upstream changed while this write waited for the lock"
    assert not target.exists(), "nothing may be persisted when the hash check fails"
    # The upstream on disk is the NEW one, and the payload carried the OLD
    # hash -- so the refusal can only have come from re-reading upstream after
    # the wait. A check performed before acquiring the lock would have compared
    # against the old contract and let this through.
    assert cc.upstream_hash(json.loads(upstream.read_text(encoding="utf-8"))) != old_hash


def test_lock_is_released_after_a_refused_downstream_write(seeded_case, real_reasons, tmp_path):
    """A refusal must not strand the lock -- the next writer would otherwise
    block for the full 15-minute wait on a file nobody holds."""
    stale = cc.upstream_hash(_meaningfully_changed(real_reasons))
    rc = dao.cmd_write_contract(_write_args(
        tmp_path, _screening(stale), "screening_report.json",
        "screening_report.schema.json"))

    assert rc == 1
    assert not list(seeded_case.glob("*.lock"))
