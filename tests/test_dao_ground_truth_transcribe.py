"""read-ground-truth --transcribe: the sanctioned read path for a SCANNED
answer key (known-gaps item 40b).

A scanned ground-truth PDF had no legitimate read path at all. The OCR pipeline
is deliberately closed to ground truth (it writes into data/processed, which
non-evaluation stages read) and the 2026-07-22 Read deny-glob closed direct
opening -- together leaving the one stage D1 exempts unable to read a scan by
any sanctioned means. On CASE_907 that forced rendering pages to .tmp/ and
reading them there: it escaped the deny-glob (bound to a PATH, so copying out
defeats it), passed no gate, and left no record.

The two properties under test are therefore:
  1. authorization is unchanged -- --transcribe grants nothing to a caller the
     gate already denies, and denial happens before any bytes are touched; and
  2. nothing persists -- the rendered pages are gone afterwards, including when
     transcription raises partway through.
"""
import glob
import os
import tempfile
from pathlib import Path

import pytest

import dao


class _Args:
    def __init__(self, case_id="CASE_907", caller_stage="evaluation", version="v2",
                 file="GT_001", transcribe=True, list=False):
        self.case_id, self.caller_stage, self.version = case_id, caller_stage, version
        self.file, self.transcribe, self.list = file, transcribe, list


@pytest.fixture
def scanned_gt(isolated_dao, monkeypatch):
    """A ground-truth PDF with no embedded text layer, plus the review flag the
    gate requires, in an isolated tree."""
    gt_dir = isolated_dao / "data" / "ground_truth" / "CASE_907"
    gt_dir.mkdir(parents=True, exist_ok=True)
    (gt_dir / "GT_001.pdf").write_bytes(b"%PDF-1.4 fake scan, no text layer")
    monkeypatch.setattr(dao, "pdf_embedded_page_texts", lambda p: None, raising=False)
    import ocr_extract
    monkeypatch.setattr(ocr_extract, "pdf_embedded_page_texts", lambda p: None)
    flag = dao.human_review_flag_path("CASE_907", "v2")
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("{}", encoding="utf-8")
    return isolated_dao


def _leftover_render_dirs():
    return glob.glob(os.path.join(tempfile.gettempdir(), "gt_transcribe_*"))


# ---- 1. authorization is unchanged ----

def test_transcribe_denied_for_non_evaluation_caller(scanned_gt, capsys):
    """--transcribe must not become a side door around D1."""
    rc = dao.cmd_read_ground_truth(_Args(caller_stage="critic"))
    assert rc == 1
    assert "DENIED" in capsys.readouterr().out
    assert _leftover_render_dirs() == []


def test_transcribe_denied_without_review_flag(scanned_gt, capsys):
    dao.human_review_flag_path("CASE_907", "v2").unlink()
    rc = dao.cmd_read_ground_truth(_Args())
    assert rc == 1
    assert "DENIED" in capsys.readouterr().out


def test_denial_happens_before_any_render(scanned_gt, monkeypatch, capsys):
    """The gate must reject before the file is touched at all -- not render
    first and check after."""
    def explode(*a, **k):
        raise AssertionError("rendered before the authorization gate ran")
    import ocr_extract
    monkeypatch.setattr(ocr_extract, "split_to_page_images", explode)
    assert dao.cmd_read_ground_truth(_Args(caller_stage="draft-report")) == 1


# ---- 2. nothing persists ----

def _stub_provider(monkeypatch, pages=2, transcribe=None):
    import llm_providers, ocr_extract

    class _Result:
        def __init__(self, text): self.text = text

    class _Provider:
        def transcribe_image(self, image_path, prompt, version):
            if transcribe is not None:
                return transcribe(image_path)
            return _Result(f"text of {Path(image_path).name}")

    monkeypatch.setattr(llm_providers, "build_provider", lambda *a, **k: _Provider())
    rendered = []

    def fake_render(doc_path, out_dir, max_pages=None):
        out = []
        for i in range(1, pages + 1):
            p = Path(out_dir) / f"page_{i:03d}.png"
            p.write_bytes(b"png")
            out.append(p)
        rendered.append(Path(out_dir))
        return out

    monkeypatch.setattr(ocr_extract, "split_to_page_images", fake_render)
    return rendered


def test_transcribes_every_page_and_leaves_nothing(scanned_gt, monkeypatch, capsys):
    rendered = _stub_provider(monkeypatch, pages=3)
    rc = dao.cmd_read_ground_truth(_Args())
    out = capsys.readouterr().out
    assert rc == 0
    assert out.count("<<<PAGE page=") == 3
    assert "method=ephemeral_vision" in out
    # The directory the pages were rendered into must be gone.
    assert rendered and not rendered[0].exists()
    assert _leftover_render_dirs() == []


def test_render_dir_removed_even_when_transcription_raises(scanned_gt, monkeypatch):
    """The finally is the whole point: a provider failure must not strand
    rendered answer-key images on disk."""
    def boom(image_path):
        raise RuntimeError("provider exploded")
    rendered = _stub_provider(monkeypatch, pages=2, transcribe=boom)
    with pytest.raises(RuntimeError):
        dao.cmd_read_ground_truth(_Args())
    assert rendered and not rendered[0].exists()
    assert _leftover_render_dirs() == []


def test_blank_page_reports_failure_not_silence(scanned_gt, monkeypatch, capsys):
    """An empty transcription must be stated, never emitted as a blank page an
    evaluator could read as 'this page says nothing'."""
    class _Empty:
        text = "   "
    _stub_provider(monkeypatch, pages=1, transcribe=lambda p: _Empty())
    dao.cmd_read_ground_truth(_Args())
    assert "TRANSCRIPTION_FAILED" in capsys.readouterr().out


def test_scan_without_transcribe_flag_still_refuses(scanned_gt, capsys):
    """The default stays closed; the ephemeral read is opt-in and explicit."""
    rc = dao.cmd_read_ground_truth(_Args(transcribe=False))
    out = capsys.readouterr().out
    assert rc == 1
    assert "NO_TEXT_LAYER" in out and "--transcribe" in out


def test_transcribe_rejects_unsafe_file_id(scanned_gt):
    with pytest.raises(SystemExit):
        dao.cmd_read_ground_truth(_Args(file="../../../etc/passwd"))
