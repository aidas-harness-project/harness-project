"""document_assembly.py -- render() plus the full CLI path (locking, atomic
write, sidecar schema validation) added this session after it was found
bypassing the DAO entirely. Regression coverage for the three real bugs
found while fixing it: the DAO-bypass itself, schema_name_for() not
resolving *.evidence.json, and render() emitting page: null.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import document_assembly as da
import dao


@pytest.fixture
def isolated_da(tmp_path, monkeypatch):
    monkeypatch.setattr(da, "ROOT", tmp_path)
    monkeypatch.setattr(dao, "ROOT", tmp_path)
    monkeypatch.setattr(dao, "OUTPUTS", tmp_path / "outputs")
    monkeypatch.setattr(dao, "DATA", tmp_path / "data")
    # Real processed text for the documents these specs cite. main() verifies
    # every citation quote against this layer before writing, so a fixture
    # without it would exercise the refusal path instead of the render path --
    # and seeding it keeps each end-to-end test passing THROUGH the check
    # rather than around it.
    for doc_id, text in (("DOC_001", "claim filed"),
                         ("DOC_002", "second source")):
        d = tmp_path / "data" / "processed" / "CASE_009" / doc_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "redacted_text.md").write_text(text, encoding="utf-8")
    return tmp_path


def test_render_placeholder_reference_count_must_match():
    spec = {"output_path": "x.md", "sections": [
        {"heading": "H", "content": "one {{E}} two {{E}}",
         "evidence_references": [{"document_id": "DOC_001", "quote": "q"}]},  # only 1, content has 2
    ]}
    with pytest.raises(ValueError):
        da.render(spec)


def test_render_tags_assigned_sequentially_across_sections():
    spec = {"output_path": "x.md", "sections": [
        {"heading": "A", "content": "first {{E}}", "evidence_references": [{"document_id": "DOC_001", "quote": "q1"}]},
        {"heading": "B", "content": "second {{E}}", "evidence_references": [{"document_id": "DOC_002", "quote": "q2"}]},
    ]}
    text, sidecar = da.render(spec)
    assert "[E1]" in text and "[E2]" in text
    assert [c["tag"] for c in sidecar["citations"]] == ["E1", "E2"]


def test_render_omits_page_key_when_absent():
    """Regression: used to emit page: null, which fails
    evidence_sidecar.schema.json's integer-only page type."""
    spec = {"output_path": "x.md", "sections": [
        {"heading": "A", "content": "text {{E}}", "evidence_references": [{"document_id": "DOC_001", "quote": "q"}]},
    ]}
    _, sidecar = da.render(spec)
    assert "page" not in sidecar["citations"][0]


def test_render_keeps_page_when_present():
    spec = {"output_path": "x.md", "sections": [
        {"heading": "A", "content": "text {{E}}", "evidence_references": [{"document_id": "DOC_001", "page": 3, "quote": "q"}]},
    ]}
    _, sidecar = da.render(spec)
    assert sidecar["citations"][0]["page"] == 3


def _write_sections_file(tmp_path, spec):
    p = tmp_path / "sections.json"
    p.write_text(json.dumps(spec), encoding="utf-8")
    return str(p)


def _run_main(sections_file, held_by="draft-report", run_id="RUN_X"):
    import sys
    argv = sys.argv
    sys.argv = ["document_assembly.py", "--sections-file", sections_file, "--held-by", held_by, "--run-id", run_id]
    try:
        da.main()
    finally:
        sys.argv = argv


def _run_structured_main(structured_file, output_path, template="진단수술비형"):
    import sys
    argv = sys.argv
    sys.argv = [
        "document_assembly.py",
        "--structured-report-file",
        structured_file,
        "--output-path",
        output_path,
        "--held-by",
        "draft-report",
        "--run-id",
        "RUN_X",
        "--template",
        template,
    ]
    try:
        da.main()
    finally:
        sys.argv = argv


def _write_structured_contract(root, document, case_id="CASE_009", version=1):
    """Seed the renderer through the same DAO write used by pipeline agents."""
    source = root / f"structured-input-v{version}.json"
    source.write_text(json.dumps(document), encoding="utf-8")
    filename = f"loss_adjustment_report_v{version}.json"
    rc = dao.cmd_write_contract(
        SimpleNamespace(
            case_id=case_id,
            filename=filename,
            data_file=str(source),
            schema_name="loss_adjustment_report.schema.json",
            held_by="draft-report",
            run_id="RUN_X",
            purpose="document assembly test",
            stage=None,
        )
    )
    assert rc == 0
    return f"outputs/{case_id}/{filename}"


def test_full_render_writes_md_and_valid_sidecar_and_releases_lock(isolated_da):
    spec = {"output_path": "outputs/CASE_009/draft_report_v1.md", "sections": [
        {"heading": "1. Overview", "content": "Claim {{E}} filed.",
         "evidence_references": [{"document_id": "DOC_001", "page": 1, "quote": "claim filed"}]},
    ]}
    sections_file = _write_sections_file(isolated_da, spec)

    _run_main(sections_file)

    out_path = isolated_da / "outputs" / "CASE_009" / "draft_report_v1.md"
    sidecar_path = out_path.with_suffix(".evidence.json")
    assert out_path.exists()
    assert sidecar_path.exists()
    assert not out_path.with_name(out_path.name + ".lock").exists()

    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    from _validation import load_registry, validate_instance
    schemas, registry = load_registry()
    assert validate_instance(sidecar, "evidence_sidecar.schema.json", schemas, registry) == []


def test_full_render_from_structured_contract_uses_verified_citations(isolated_da):
    project_root = Path(__file__).resolve().parent.parent
    document = json.loads(
        (
            project_root
            / "loss-adjustment-format-study"
            / "analysis"
            / "examples"
            / "example-disease-benefit.json"
        ).read_text(encoding="utf-8")
    )
    document["case_reference"]["case_id"] = "CASE_009"
    for evidence in document["evidence_registry"]:
        target = (
            isolated_da
            / "data"
            / "processed"
            / "CASE_009"
            / evidence["document_id"]
        )
        target.mkdir(parents=True, exist_ok=True)
        (target / "redacted_text.md").write_text(evidence["quote"], encoding="utf-8")
    structured_file = _write_structured_contract(isolated_da, document)
    output_path = "outputs/CASE_009/draft_report_v1.md"

    _run_structured_main(structured_file, output_path)

    rendered = isolated_da / output_path
    sidecar = rendered.with_suffix(".evidence.json")
    assert rendered.exists()
    assert sidecar.exists()
    assert "[E1]" in rendered.read_text(encoding="utf-8")


def test_structured_render_refuses_case_id_mismatch(isolated_da):
    project_root = Path(__file__).resolve().parent.parent
    document = json.loads(
        (
            project_root
            / "loss-adjustment-format-study"
            / "analysis"
            / "examples"
            / "example-disease-benefit.json"
        ).read_text(encoding="utf-8")
    )
    structured_file = _write_structured_contract(isolated_da, document)

    with pytest.raises(SystemExit, match="case_reference.case_id"):
        _run_structured_main(
            structured_file, "outputs/CASE_009/draft_report_v1.md"
        )

    assert not (isolated_da / "outputs/CASE_009/draft_report_v1.md").exists()


def test_structured_render_refuses_arbitrary_input_file(isolated_da):
    arbitrary = isolated_da / "loss_adjustment_report_v1.json"
    arbitrary.write_text("{}", encoding="utf-8")

    with pytest.raises(SystemExit, match="canonical DAO contract"):
        _run_structured_main(
            str(arbitrary), "outputs/CASE_009/draft_report_v1.md"
        )

    assert not (isolated_da / "outputs/CASE_009/draft_report_v1.md").exists()


def test_structured_render_refuses_case_or_version_path_mismatch(isolated_da):
    with pytest.raises(SystemExit, match="canonical DAO contract"):
        _run_structured_main(
            "outputs/CASE_009/loss_adjustment_report_v2.json",
            "outputs/CASE_009/draft_report_v1.md",
        )


def test_render_refuses_output_without_case_identity(isolated_da):
    spec = {
        "output_path": "outputs/draft_report_v1.md",
        "sections": [
            {
                "heading": "1. Overview",
                "content": "Claim {{E}} filed.",
                "evidence_references": [
                    {"document_id": "DOC_001", "quote": "claim filed"}
                ],
            }
        ],
    }
    sections_file = _write_sections_file(isolated_da, spec)

    with pytest.raises(SystemExit, match="must identify its case"):
        _run_main(sections_file)

    assert not (isolated_da / "outputs/draft_report_v1.md").exists()


def test_locked_target_is_not_rendered(isolated_da, monkeypatch):
    """document_assembly.py's lock now blocks-and-waits like everywhere else
    in the DAO -- keep the wait window tiny so this test doesn't actually
    sit for P5's real 15-minute cap before failing."""
    monkeypatch.setattr(dao, "LOCK_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(dao, "LOCK_MAX_WAIT_SECONDS", 0.03)
    spec = {"output_path": "outputs/CASE_009/draft_report_v1.md", "sections": [
        {"heading": "1. Overview", "content": "Claim {{E}} filed.",
         "evidence_references": [{"document_id": "DOC_001", "quote": "claim filed"}]},
    ]}
    sections_file = _write_sections_file(isolated_da, spec)
    out_path = isolated_da / "outputs" / "CASE_009" / "draft_report_v1.md"
    dao.acquire_lock(out_path, "someone-else", "RUN_OTHER", "holding")

    with pytest.raises(SystemExit):
        _run_main(sections_file)

    assert not out_path.exists()


def test_no_files_written_when_sidecar_would_fail_schema(isolated_da):
    """A reference with an empty quote fails evidence_sidecar.schema.json's
    minLength:1 -- neither the .md nor the sidecar should land on disk."""
    spec = {"output_path": "outputs/CASE_009/draft_report_v1.md", "sections": [
        {"heading": "1. Overview", "content": "Claim {{E}} filed.",
         "evidence_references": [{"document_id": "DOC_001", "quote": ""}]},
    ]}
    sections_file = _write_sections_file(isolated_da, spec)

    with pytest.raises(SystemExit):
        _run_main(sections_file)

    out_path = isolated_da / "outputs" / "CASE_009" / "draft_report_v1.md"
    assert not out_path.exists()
    assert not out_path.with_suffix(".evidence.json").exists()


# --- template enforcement (--template / validate_template) --------------------

def _headings_b():
    """A conforming 변형 B (진단수술비형) heading set, as CASE_021 v2 actually used."""
    return ["표지 · 제출 공문 · 속표지 (양식 고정부)", "I. 사정 요약", "II. 위임 및 보험계약사항",
            "III. 보험사고 발생의 조사·확인한 사실", "IV. 관계법규 및 약관의 적용·판단",
            "V. 보험금 사정", "VI. 증빙자료"]


def test_template_conforming_headings_pass():
    assert da.validate_template(_headings_b(), "진단수술비형") == []


def test_template_missing_section_fails():
    headings = _headings_b()
    del headings[4]  # drop IV
    errors = da.validate_template(headings, "진단수술비형")
    assert errors, "a missing required section must be an error"


def test_template_misordered_sections_fail():
    headings = _headings_b()
    headings[1], headings[2] = headings[2], headings[1]  # swap I and II
    errors = da.validate_template(headings, "진단수술비형")
    assert errors, "sections in the wrong order must be an error"


def test_template_extra_section_fails_when_not_allowed():
    headings = _headings_b() + ["VII. 임의 추가 섹션"]
    errors = da.validate_template(headings, "진단수술비형")
    assert errors, "an extra section must be an error when allow_extra_sections is false"


def test_template_unknown_key_fails():
    errors = da.validate_template(_headings_b(), "no_such_template")
    assert errors and "unknown template" in errors[0]


def test_structured_report_converts_to_template_sections_and_citations():
    document = json.loads(
        (
            da.ROOT
            / "loss-adjustment-format-study"
            / "analysis"
            / "examples"
            / "example-disease-benefit.json"
        ).read_text(encoding="utf-8")
    )

    spec = da.sections_from_structured_report(
        document,
        "진단수술비형",
        "outputs/CASE_009/draft_report_v1.md",
    )

    headings = [section["heading"] for section in spec["sections"]]
    assert da.validate_template(headings, "진단수술비형") == []
    assert len(headings) == 7
    assert spec["sections"][0]["evidence_references"] == []
    assert "{{E}}" in spec["sections"][1]["content"]
    assert spec["sections"][1]["evidence_references"][0] == {
        "document_id": "DOC_101",
        "page": 7,
        "quote": "특별약관에 질병 정의와 진단확정 요건이 기재되어 있음.",
    }


def test_structured_report_refuses_a_template_from_another_family():
    document = json.loads(
        (
            da.ROOT
            / "loss-adjustment-format-study"
            / "analysis"
            / "examples"
            / "example-disease-benefit.json"
        ).read_text(encoding="utf-8")
    )

    with pytest.raises(ValueError, match="does not accept family"):
        da.sections_from_structured_report(
            document,
            "배상책임_후유장해형",
            "outputs/CASE_009/draft_report_v1.md",
        )


def test_template_screening_report_conforms():
    headings = ["1. 사건 개요", "2. 보험사 판단", "3. 핵심 쟁점", "4. 문서 간 불일치",
                "5. 추가 필요 서류", "6. 전문가 검수 포인트", "7. 1차 판단"]
    assert da.validate_template(headings, "screening_report") == []


# --------------------------------------------------- citation quote check ---
# The tool renders whatever it is handed. Auto-generating the [E#] tags and the
# sidecar from one source stops a tag and its citation DRIFTING, but says
# nothing about whether the quote is real: an invented citation renders
# cleanly, passes read-evidence-tags (consistent, 0 orphaned, 0 unused), and
# ships. On CASE_909's v2 draft, 29 of 190 citations were wrong -- quotes
# recalled rather than copied, and bills attributed to the wrong document --
# caught only because that agent wrote its own verifier first.

def _processed(tmp_path, monkeypatch, case_id="CASE_909", docs=None):
    monkeypatch.setattr(da, "ROOT", tmp_path)
    for doc_id, text in (docs or {}).items():
        d = tmp_path / "data" / "processed" / case_id / doc_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "redacted_text.md").write_text(text, encoding="utf-8")


def _sidecar(*citations):
    return {"citations": [
        {"tag": f"E{i}", "document_id": d, "page": p, "quote": q}
        for i, (d, p, q) in enumerate(citations, start=1)]}


def test_quote_present_in_source_passes(tmp_path, monkeypatch):
    _processed(tmp_path, monkeypatch,
               docs={"DOC_001": "<<<PAGE page=1>>>\n계단은 그리 넓지 않은바\n"})
    side = _sidecar(("DOC_001", 1, "계단은 그리 넓지 않은바"))
    assert da.verify_citation_quotes(side, "CASE_909") == []


def test_invented_quote_is_rejected(tmp_path, monkeypatch):
    _processed(tmp_path, monkeypatch,
               docs={"DOC_001": "<<<PAGE page=1>>>\n계단은 그리 넓지 않은바\n"})
    side = _sidecar(("DOC_001", 1, "이 문장은 어느 문서에도 없습니다"))
    errors = da.verify_citation_quotes(side, "CASE_909")
    assert len(errors) == 1
    assert "quote not found" in errors[0]


def test_right_quote_wrong_document_is_rejected(tmp_path, monkeypatch):
    """The real CASE_909 failure: the words existed, in another document."""
    _processed(tmp_path, monkeypatch, docs={
        "DOC_015": "<<<PAGE page=1>>>\n본인부담금 246,560\n",
        "DOC_014": "<<<PAGE page=1>>>\n총진료비 5,806,892\n",
    })
    side = _sidecar(("DOC_014", 1, "본인부담금 246,560"))
    assert len(da.verify_citation_quotes(side, "CASE_909")) == 1


def test_line_wrapped_quote_still_passes(tmp_path, monkeypatch):
    """Extraction wraps mid-sentence, so a quote spanning a wrap is a REAL
    quote. Failing it would train callers to cite only short fragments, which
    makes citations less checkable, not more."""
    _processed(tmp_path, monkeypatch,
               docs={"DOC_001": "제설제인 염화칼슘이나 모\n래 등을 뿌려\n"})
    side = _sidecar(("DOC_001", 9, "제설제인 염화칼슘이나 모래 등을 뿌려"))
    assert da.verify_citation_quotes(side, "CASE_909") == []


def test_document_with_no_processed_text_is_rejected(tmp_path, monkeypatch):
    """A citation cannot point at a document this case never processed --
    e.g. a superseded bundle, whose pages belong to its children."""
    _processed(tmp_path, monkeypatch, docs={"DOC_001": "본문\n"})
    side = _sidecar(("DOC_005", 1, "무엇이든"))
    errors = da.verify_citation_quotes(side, "CASE_909")
    assert len(errors) == 1
    assert "no processed text" in errors[0]


def test_every_bad_citation_is_reported_not_just_the_first(tmp_path, monkeypatch):
    """29 were wrong at once. Reporting one per run would take 29 renders."""
    _processed(tmp_path, monkeypatch, docs={"DOC_001": "본문\n"})
    side = _sidecar(("DOC_001", 1, "없음1"), ("DOC_001", 2, "없음2"),
                    ("DOC_001", 3, "없음3"))
    assert len(da.verify_citation_quotes(side, "CASE_909")) == 3


def test_main_refuses_a_bad_citation_and_writes_nothing(isolated_da):
    """Drives main(), not the helper.

    Every test above passes if the check is computed and then never consulted
    -- the "implemented but not wired" shape this project keeps hitting. It
    also pins the fail-BEFORE-disk contract: a document whose citations do not
    resolve must not exist, because read-evidence-tags and
    check-untagged-claims both read the tag layer and would report it clean.
    """
    spec = {"output_path": "outputs/CASE_009/draft_report_v1.md", "sections": [
        {"heading": "1. Overview", "content": "Claim {{E}} filed.",
         "evidence_references": [
             {"document_id": "DOC_001", "page": 1,
              "quote": "이 문장은 처리된 원문에 없습니다"}]},
    ]}
    sections_file = _write_sections_file(isolated_da, spec)

    with pytest.raises(SystemExit) as excinfo:
        _run_main(sections_file)
    assert "citation quotes do not appear" in str(excinfo.value)

    out_path = isolated_da / "outputs" / "CASE_009" / "draft_report_v1.md"
    assert not out_path.exists()
    assert not out_path.with_suffix(".evidence.json").exists()
    assert not out_path.with_name(out_path.name + ".lock").exists()
