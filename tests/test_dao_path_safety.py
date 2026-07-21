"""Path-traversal containment for the DAO (fleet review C1).

A crafted case_id/doc_id/filename must not escape outputs/ or data/ -- the DAO
is the sole safe boundary, and a traversal there defeats D1 (reach
data/ground_truth) and raw-source immutability (write into source-cases)."""
import pytest

import dao


@pytest.mark.parametrize("bad", ["../..", "../../data", "CASE/../..", "..", "a/b", "CASE 1", ""])
def test_case_dir_rejects_unsafe_case_id(bad):
    with pytest.raises(SystemExit):
        dao.case_dir(bad)


@pytest.mark.parametrize("bad_doc", ["../..", "DOC/../x", "a/b", ""])
def test_processed_dir_rejects_unsafe_doc_id(bad_doc):
    with pytest.raises(SystemExit):
        dao.processed_dir("CASE_001", bad_doc)


def test_require_within_blocks_filename_traversal(tmp_path):
    base = tmp_path / "outputs" / "CASE_001"
    base.mkdir(parents=True)
    with pytest.raises(SystemExit):
        dao._require_within(base, "../../secret.json")
    with pytest.raises(SystemExit):
        dao._require_within(base, "/etc/passwd")
    with pytest.raises(SystemExit):
        dao._require_within(base, "a\x00b")


def test_require_within_allows_contained_paths(tmp_path):
    base = tmp_path / "outputs" / "CASE_001"
    base.mkdir(parents=True)
    # flat filename and a legit subdir (e.g. a backup path) both resolve inside
    assert dao._require_within(base, "ocr_result_DOC_001.json") == base / "ocr_result_DOC_001.json"
    assert dao._require_within(base, "_backups/step_01/x.json") == base / "_backups/step_01/x.json"


def test_read_contract_traversal_blocked(monkeypatch, capsys, tmp_path):
    # end-to-end: the read-contract command refuses a traversal filename
    monkeypatch.setattr(dao, "OUTPUTS", tmp_path / "outputs")
    (tmp_path / "outputs").mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET", encoding="utf-8")
    args = type("A", (), {"case_id": "CASE_001", "filename": "../../secret.txt"})()
    with pytest.raises(SystemExit):
        dao.cmd_read_contract(args)
    assert "TOP SECRET" not in capsys.readouterr().out
