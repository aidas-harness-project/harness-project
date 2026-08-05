"""The classification_review human-review kind, and the document_processing
gate that consumes it.

Background. checkpoint 1 classifies from redacted_text.md when it exists and
falls back to the pre-redaction page_NNN.md when it does not -- deliberate,
since a document that has not been redacted yet must still be classifiable.
The fallback records `classification_text_source: raw_page_text` and sets
`review_required`, and the schema promised "a reviewer sees that unredacted
text reached an analysis stage".

Nothing kept that promise. No finalize check read the flag and
record-human-review had no artifact kind that could clear it, so a case could
pass every stage carrying "a human must look at this" forever. These tests pin
both halves: the flag now blocks document_processing, and there is a real,
hash-bound way to clear it.
"""
import copy
import json

import pytest

import dao
import human_review


@pytest.fixture(autouse=True)
def fast_lock_wait(monkeypatch):
    monkeypatch.setattr(dao, "LOCK_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(dao, "LOCK_MAX_WAIT_SECONDS", 0.05)


TARGET_KEY = "classification_text_source:raw_page_text"


def _classification(doc_id="DOC_001", text_source="raw_page_text",
                    review_required=True, label="insurer_response"):
    out = {
        "case_id": "CASE_009",
        "component": "document-pipeline",
        "status": "success",
        "created_at": "2026-08-05T20:03:09.402573+09:00",
        "run_id": "RUN_20260712_001",
        "document_id": doc_id,
        "predicted_document_type": label,
        "confidence": 0.93,
        "pre_flagged": False,
        "evidence_references": [{"page": 1, "quote": "제목: 손해사정 업무 협조요청"}],
        "review_required": review_required,
        "classification_text_source": text_source,
    }
    if review_required:
        # The schema requires a reviewer_role wherever review_required is set.
        out["reviewer_role"] = "손해사정사"
    return out


def _seed(isolated_dao, *docs):
    """Write classification files + a manifest naming them."""
    case = isolated_dao / "outputs" / "CASE_009"
    case.mkdir(parents=True, exist_ok=True)
    for c in docs:
        (case / f"classification_result_{c['document_id']}.json").write_text(
            json.dumps(c, ensure_ascii=False), encoding="utf-8")
    manifest = {"documents": [{"document_id": c["document_id"]} for c in docs]}
    (case / "document_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    return manifest


def _record(make_args, doc_id="DOC_001", decision="verified"):
    return dao.cmd_record_human_review(make_args(
        artifact_kind="classification_review", artifact_id=doc_id,
        target_key=TARGET_KEY, decision=decision,
        reviewer="pyun", note="quote survives redaction"))


def _uid_for(isolated_dao, doc_id="DOC_001"):
    ledger = dao.load_human_review_ledger("CASE_009")
    return [r for r in ledger["records"] if r["artifact_id"] == doc_id][-1]["review_uid"]


def _set_uid(isolated_dao, uid, doc_id="DOC_001"):
    path = (isolated_dao / "outputs" / "CASE_009"
            / f"classification_result_{doc_id}.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["human_review_uid"] = uid
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# ------------------------------------------------------------ the gate ------

def test_raw_page_text_classification_blocks_without_review(isolated_dao):
    manifest = _seed(isolated_dao, _classification())
    blockers = dao._classification_review_blockers("CASE_009", manifest)
    assert len(blockers) == 1
    assert "DOC_001" in blockers[0]
    assert "no human_review_uid" in blockers[0]


def test_redacted_text_classification_never_blocks(isolated_dao):
    """The normal path -- a split child inheriting its bundle's redaction --
    must not be dragged into a review it never needed."""
    manifest = _seed(isolated_dao, _classification(
        text_source="redacted_text", review_required=False))
    assert dao._classification_review_blockers("CASE_009", manifest) == []


def test_review_clears_the_gate(isolated_dao, make_args):
    manifest = _seed(isolated_dao, _classification())
    assert _record(make_args) == 0
    _set_uid(isolated_dao, _uid_for(isolated_dao))
    assert dao._classification_review_blockers("CASE_009", manifest) == []


def test_rejected_decision_does_not_clear_the_gate(isolated_dao, make_args):
    """A rejection is recorded so it is durable, but it is not an acceptance."""
    manifest = _seed(isolated_dao, _classification())
    assert _record(make_args, decision="rejected") == 0
    _set_uid(isolated_dao, _uid_for(isolated_dao))
    blockers = dao._classification_review_blockers("CASE_009", manifest)
    assert len(blockers) == 1
    assert "rejected" in blockers[0]


def test_editing_the_label_after_review_invalidates_it(isolated_dao, make_args):
    """The point of hash binding: a review covers the bytes it was made
    against, so re-labelling the document afterwards must not ride on it."""
    manifest = _seed(isolated_dao, _classification())
    assert _record(make_args) == 0
    _set_uid(isolated_dao, _uid_for(isolated_dao))
    assert dao._classification_review_blockers("CASE_009", manifest) == []

    path = (isolated_dao / "outputs" / "CASE_009"
            / "classification_result_DOC_001.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["predicted_document_type"] = "insurance_policy"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    blockers = dao._classification_review_blockers("CASE_009", manifest)
    assert len(blockers) == 1
    assert "changed after review" in blockers[0]


def test_uid_from_another_document_does_not_transfer(isolated_dao, make_args):
    manifest = _seed(isolated_dao, _classification("DOC_001"),
                     _classification("DOC_002"))
    assert _record(make_args, doc_id="DOC_001") == 0
    # Point DOC_002 at DOC_001's review.
    _set_uid(isolated_dao, _uid_for(isolated_dao, "DOC_001"), doc_id="DOC_002")
    blockers = dao._classification_review_blockers("CASE_009", manifest)
    assert any("DOC_002" in b and "recorded for document" in b for b in blockers)


def test_fabricated_uid_is_refused(isolated_dao):
    manifest = _seed(isolated_dao, _classification())
    _set_uid(isolated_dao, "HR-" + "0" * 16)
    blockers = dao._classification_review_blockers("CASE_009", manifest)
    assert len(blockers) == 1
    assert "does not exist" in blockers[0]


# -------------------------------------- the gate is actually WIRED IN --------

def _seed_for_finalize(isolated_dao, make_args, **kwargs):
    """A case whose ONLY document_processing blocker is the review flag.

    Every other check in that gate reads the manifest, so the manifest is
    written through the real write path and the classification is made
    consistent with it -- otherwise a refusal here could come from some
    unrelated blocker and the test would pass for the wrong reason.
    """
    classification = _classification(**kwargs)
    case = isolated_dao / "outputs" / "CASE_009"
    case.mkdir(parents=True, exist_ok=True)
    (case / "classification_result_DOC_001.json").write_text(
        json.dumps(classification, ensure_ascii=False), encoding="utf-8")
    (case / "document_manifest.json").write_text(json.dumps({
        "case_id": "CASE_009",
        "documents": [{
            "document_id": "DOC_001",
            "document_type": classification["predicted_document_type"],
            "classification_confidence": classification["confidence"],
            "ocr_status": "completed",
        }],
    }, ensure_ascii=False), encoding="utf-8")
    dao.cmd_update_run_state(make_args(stage="intake", status="in_progress"))
    dao.cmd_finalize_stage(make_args(stage="intake"))
    dao.cmd_update_run_state(
        make_args(stage="document_processing", status="in_progress"))


def test_finalize_document_processing_refuses_through_the_real_cli(
        isolated_dao, make_args, capsys):
    """Not the helper -- cmd_finalize_stage itself.

    Every other test here calls _classification_review_blockers directly, so
    all of them keep passing if the gate is computed and then never consulted.
    That is the precise failure this whole change exists to fix, so one test
    has to go through the command a run actually invokes.
    """
    _seed_for_finalize(isolated_dao, make_args)
    rc = dao.cmd_finalize_stage(make_args(stage="document_processing"))
    assert rc == 1
    out = capsys.readouterr().out
    assert "PRE-REDACTION" in out
    assert "DOC_001" in out
    assert "record-human-review" in out


def test_finalize_document_processing_proceeds_once_reviewed(
        isolated_dao, make_args, capsys):
    _seed_for_finalize(isolated_dao, make_args)
    assert _record(make_args) == 0
    _set_uid(isolated_dao, _uid_for(isolated_dao))
    assert dao.cmd_finalize_stage(make_args(stage="document_processing")) == 0


# ------------------------------------------- record-human-review guards ------

def test_cannot_review_a_document_that_read_redacted_text(isolated_dao, make_args):
    """Filing a review for a condition that did not occur would make the
    ledger claim a human inspected something nobody ever needed to."""
    _seed(isolated_dao, _classification(
        text_source="redacted_text", review_required=False))
    assert _record(make_args) == 1


def test_wrong_target_key_is_refused(isolated_dao, make_args):
    _seed(isolated_dao, _classification())
    rc = dao.cmd_record_human_review(make_args(
        artifact_kind="classification_review", artifact_id="DOC_001",
        target_key="something_else", decision="verified",
        reviewer="pyun", note="n"))
    assert rc == 1


def test_missing_artifact_is_refused(isolated_dao, make_args):
    (isolated_dao / "outputs" / "CASE_009").mkdir(parents=True, exist_ok=True)
    assert _record(make_args) == 1


# ------------------------------------------------- the canonical-hash rule ---

def test_writing_the_uid_does_not_invalidate_the_review():
    """The round trip the whole mechanism depends on: record against an
    artifact with no human_review_uid, then write the returned UID into it.

    Under the old nulling rule the artifact went from `{...}` to
    `{..., "human_review_uid": null}` -- different canonical bytes -- so the
    record invalidated the moment it was used. Found live on CASE_909.
    """
    before = _classification()
    after = copy.deepcopy(before)
    after["human_review_uid"] = "HR-" + "a" * 16
    assert (human_review.canonical_artifact_sha256(before)
            == human_review.canonical_artifact_sha256(after))


def test_absent_and_nulled_disposition_hash_identically():
    absent = _classification()
    nulled = copy.deepcopy(absent)
    nulled["human_review_uid"] = None
    assert (human_review.canonical_artifact_sha256(absent)
            == human_review.canonical_artifact_sha256(nulled))


def test_substantive_edits_still_change_the_hash():
    """Removing disposition fields must not make the binding toothless."""
    base = _classification()
    for field, value in [("predicted_document_type", "insurance_policy"),
                         ("confidence", 0.1),
                         ("evidence_references", [{"page": 9, "quote": "x"}])]:
        edited = copy.deepcopy(base)
        edited[field] = value
        assert (human_review.canonical_artifact_sha256(base)
                != human_review.canonical_artifact_sha256(edited)), field


def test_legacy_null_hash_still_computable_for_old_records():
    """Retained purely so a review recorded under the old rule keeps
    verifying; it must stay distinct from the new rule, or the fallback would
    silently be a no-op."""
    art = _classification()
    art["human_review_uid"] = None
    assert (human_review.legacy_null_artifact_sha256(art)
            != human_review.canonical_artifact_sha256(art))
