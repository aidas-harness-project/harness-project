"""The redaction boundary: which DAO read path is safe for which stage.

Found during the CASE_901 run. `denial-response` called `read-document-text`,
got back a one-line *path* where its spec had promised text, concluded the
command had failed, and fell back to `read-page-text` -- which serves
checkpoint 1 output, before redaction. It read the claimant-facing PII that
checkpoint 2 exists to remove (a 대표이사's name, a street address, phone and
fax numbers). Nothing structural stopped it; no PII happened to reach the
output's evidence quotes, which was luck, not design.

The specs now say so explicitly, but a spec is a prompt. These tests pin the
part that can actually be enforced: the two commands are NOT interchangeable,
and what separates them is exactly the redaction pass.

The PII strings below are the real ones from CASE_901/DOC_001, kept verbatim
so a regression reproduces the original finding rather than a paraphrase of it.
"""
import os
import subprocess
import sys

import pytest

import dao
import redaction as rd


PRE_REDACTION_PAGE = """\
※ 소비자정책팀 : 서울시 서초구 서초대로74길 14, 33층 소비자보호파트(우 : 06620)
※ 상담전화: (02)758-7755   팩스: (02)758-7766
대표이사    나 채 범
"""

REDACTED_PAGE = """\
※ 소비자정책팀 : [ADDRESS]
※ 상담전화: [PHONE_NUMBER]   팩스: [PHONE_NUMBER]
대표이사    [PERSON_NAME]
"""

PII_VALUES = [
    "서울시 서초구 서초대로74길 14",
    "(02)758-7755",
    "(02)758-7766",
    "나 채 범",
]


@pytest.fixture
def processed_document(isolated_dao):
    """A document that has cleared both checkpoints, as the pipeline leaves it.

    checkpoint 1 -> page_NNN.md (pre-redaction)
    checkpoint 2 -> redacted_text.md (post-redaction, page-marked)
    """
    processed = isolated_dao / "data" / "processed" / "CASE_009" / "DOC_001"
    processed.mkdir(parents=True)
    (processed / "page_001.md").write_text(PRE_REDACTION_PAGE, encoding="utf-8")
    (processed / "redacted_text.md").write_text(
        f"<<<PAGE page=1>>>\n{REDACTED_PAGE}", encoding="utf-8"
    )
    return processed


@pytest.fixture
def checkpoint2_capability(monkeypatch):
    """Grant checkpoint 2's capability for tests that assert what the
    AUTHORIZED caller sees. Tests about refusing unauthorized callers
    deliberately do not use it."""
    token, path = dao._issue_page_text_capability("CASE_009", "DOC_001")
    monkeypatch.setenv(dao.PAGE_TEXT_CAPABILITY_ENV, token)
    yield token
    dao.release_page_text_capability(path)


def test_page_text_is_pre_redaction_and_redacted_text_is_not(processed_document):
    """The invariant the whole boundary rests on.

    If a change ever makes checkpoint 1's page files carry redacted content --
    or checkpoint 2's output carry raw content -- the distinction the specs
    draw between these two read paths stops meaning anything.
    """
    raw = (processed_document / "page_001.md").read_text(encoding="utf-8")
    redacted = (processed_document / "redacted_text.md").read_text(encoding="utf-8")

    for pii in PII_VALUES:
        assert pii in raw, f"{pii!r} should be present pre-redaction"
        assert pii not in redacted, f"{pii!r} leaked past redaction"


def test_read_page_text_returns_pre_redaction_content(
    processed_document, checkpoint2_capability, make_args, capsys
):
    """read-page-text hands back raw text -- correct for its one caller
    (redact_document.py, which feeds it straight to the Redactor) and exactly
    why no analysis stage may use it."""
    rc = dao.cmd_read_page_text(make_args(page=1, caller_stage="document-pipeline"))

    out = capsys.readouterr().out
    assert rc == 0
    assert "나 채 범" in out
    assert "[PERSON_NAME]" not in out


def test_read_document_text_returns_a_path_not_the_text(
    processed_document, make_args, capsys
):
    """The contract that got misread during CASE_901.

    A one-line path IS the success case here -- callers read that path. This
    test exists so nobody "fixes" the command into printing text on the theory
    that the path return was the bug.
    """
    rc = dao.cmd_read_document_text(make_args())

    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert out.endswith("redacted_text.md")
    assert "소비자정책팀" not in out, "the command must not emit document content"


def test_the_path_read_document_text_returns_holds_redacted_content(
    processed_document, make_args, capsys
):
    """Following the documented path must land an analysis stage on redacted
    text -- the actual end-to-end guarantee for denial-response/policy-pipeline."""
    dao.cmd_read_document_text(make_args())
    path = capsys.readouterr().out.strip()

    content = dao.Path(path).read_text(encoding="utf-8")

    assert "[PERSON_NAME]" in content
    for pii in PII_VALUES:
        assert pii not in content


def test_the_two_commands_are_not_interchangeable(
    processed_document, checkpoint2_capability, make_args, capsys
):
    """One returns a path, the other returns text, and only one of the two is
    redacted. Any change collapsing that difference re-opens the bypass."""
    dao.cmd_read_document_text(make_args())
    document_out = capsys.readouterr().out
    dao.cmd_read_page_text(make_args(page=1, caller_stage="document-pipeline"))
    page_out = capsys.readouterr().out

    assert document_out != page_out
    assert not any(pii in document_out for pii in PII_VALUES)
    assert any(pii in page_out for pii in PII_VALUES)


ANALYSIS_STAGES = [
    "denial-response",      # the stage that actually did this in CASE_901
    "policy-pipeline",
    "claim-analysis",
    "denial-validation",
    "draft-report",
    "critic",
    "evaluation",
]


@pytest.mark.parametrize("stage", ANALYSIS_STAGES)
def test_dao_denies_pre_redaction_page_text_to_analysis_stages(
    stage, processed_document, make_args, capsys
):
    """The structural half of the fix, and the one the specs cannot provide.

    Previously read-page-text had no caller check at all: any stage that
    called it got pre-redaction text back. The agent specs were corrected to
    forbid it, but a spec is a prompt -- it cannot stop the call, and in
    CASE_901 it did not. This asserts the DAO itself refuses, which is what
    read-ground-truth has always done for the D1 boundary.
    """
    rc = dao.cmd_read_page_text(make_args(page=1, caller_stage=stage))
    out = capsys.readouterr().out

    assert rc == 1, f"{stage} must be refused pre-redaction page text"
    assert "DENIED" in out
    for pii in PII_VALUES:
        assert pii not in out, f"a denial must not leak the very PII it withholds ({pii})"


def test_dao_denial_does_not_depend_on_the_page_existing(isolated_dao, make_args, capsys):
    """The caller check must run BEFORE any filesystem lookup, so an
    unauthorized stage cannot use the difference between 'denied' and
    'not extracted' to probe which pages exist."""
    rc = dao.cmd_read_page_text(make_args(page=999, caller_stage="denial-response"))
    out = capsys.readouterr().out

    assert rc == 1
    assert "DENIED" in out
    assert "NOT_EXTRACTED" not in out


def test_checkpoint2_owner_still_reads_page_text(processed_document, checkpoint2_capability, make_args, capsys):
    """The gate must not break the one stage that legitimately needs this --
    redaction cannot run without its own input."""
    rc = dao.cmd_read_page_text(make_args(page=1, caller_stage="document-pipeline"))
    out = capsys.readouterr().out

    assert rc == 0
    assert any(pii in out for pii in PII_VALUES), "checkpoint 2 reads pre-redaction text by design"


DAO_PATH = dao.ROOT / "tools" / "dao.py"


def _run_dao_cli(args, env_extra=None, cwd=None):
    """Run the DAO as a real subprocess -- the way an agent would.

    In-process tests cannot show what an agent can actually do: the gate here
    reads os.environ, so exercising it honestly means a separate process with
    a chosen environment.
    """
    env = dict(os.environ)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(DAO_PATH), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", cwd=str(cwd or dao.ROOT), env=env)


@pytest.fixture
def real_page(tmp_path, monkeypatch):
    """A processed page in an isolated tree, addressed through the DAO."""
    monkeypatch.setattr(dao, "OUTPUTS", tmp_path / "outputs")
    monkeypatch.setattr(dao, "DATA", tmp_path / "data")
    processed = tmp_path / "data" / "processed" / "CASE_009" / "DOC_001"
    processed.mkdir(parents=True)
    (processed / "page_001.md").write_text(PRE_REDACTION_PAGE, encoding="utf-8")
    return tmp_path


def test_caller_stage_alone_does_not_unlock_page_text(real_page, make_args, capsys):
    """--caller-stage is SELF-ASSERTED: any caller can type the authorized
    stage name. Naming it must not be enough on its own."""
    rc = dao.cmd_read_page_text(make_args(page=1, caller_stage="document-pipeline"))
    out = capsys.readouterr().out

    assert rc == 1
    assert "self-asserted" in out
    for pii in PII_VALUES:
        assert pii not in out


def test_capability_unlocks_page_text_for_its_own_document(real_page, make_args, monkeypatch,
                                                           capsys):
    token, path = dao._issue_page_text_capability("CASE_009", "DOC_001")
    try:
        monkeypatch.setenv(dao.PAGE_TEXT_CAPABILITY_ENV, token)
        rc = dao.cmd_read_page_text(make_args(page=1, caller_stage="document-pipeline"))
        out = capsys.readouterr().out
    finally:
        dao.release_page_text_capability(path)

    assert rc == 0
    assert any(pii in out for pii in PII_VALUES), "checkpoint 2 reads pre-redaction text by design"


def test_capability_does_not_transfer_to_another_document(real_page, make_args, monkeypatch,
                                                          capsys):
    """Scope is bound into the token's digest, so a capability minted for one
    document cannot be replayed against another."""
    token, path = dao._issue_page_text_capability("CASE_009", "DOC_999")
    try:
        monkeypatch.setenv(dao.PAGE_TEXT_CAPABILITY_ENV, token)
        rc = dao.cmd_read_page_text(make_args(page=1, caller_stage="document-pipeline"))
    finally:
        dao.release_page_text_capability(path)

    assert rc == 1


def test_capability_is_revoked_after_release(real_page, make_args, monkeypatch):
    """A token that leaked out of a finished run must not still work."""
    token, path = dao._issue_page_text_capability("CASE_009", "DOC_001")
    dao.release_page_text_capability(path)
    monkeypatch.setenv(dao.PAGE_TEXT_CAPABILITY_ENV, token)

    assert dao.cmd_read_page_text(make_args(page=1, caller_stage="document-pipeline")) == 1


@pytest.mark.parametrize("env_extra", [
    {},
    {"HARNESS_CHECKPOINT2_CAPABILITY": "guessed-value"},
    # The attack that defeated a first version of this gate: it compared one
    # environment variable against another, so a caller setting both to the
    # same value passed. Verification now depends on a file only the issuing
    # process could have created.
    {"HARNESS_CHECKPOINT2_CAPABILITY": "x", "HARNESS_CHECKPOINT2_CAPABILITY_EXPECTED": "x"},
], ids=["no-capability", "guessed-token", "self-consistent-env-pair"])
def test_spoofed_capability_is_refused_end_to_end(env_extra):
    """Run as a real subprocess against the real repo, the way an agent
    would -- claiming the stage name and forging the environment."""
    result = _run_dao_cli(
        ["read-page-text", "CASE_903", "DOC_001", "4", "--caller-stage", "document-pipeline"],
        env_extra=env_extra)

    assert result.returncode == 1, f"spoof succeeded with {env_extra}"
    assert "DENIED" in result.stdout
    assert "나 채 범" not in result.stdout


def test_expert_review_only_document_is_refused_before_any_path_is_emitted(
    isolated_dao, make_args, capsys
):
    """The gate still holds for non-text visual evidence: a blocked document
    must not yield a path an agent could open (P8's expert_review_only route)."""
    processed = isolated_dao / "data" / "processed" / "CASE_009" / "DOC_001"
    processed.mkdir(parents=True)
    (processed / "redacted_text.md").write_text("should never be reachable", encoding="utf-8")

    manifest_path = isolated_dao / "outputs" / "CASE_009" / "document_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    dao.atomic_write_json(manifest_path, {
        "case_id": "CASE_009",
        "documents": [{
            "document_id": "DOC_001",
            "file_name": "DOC_001.pdf",
            "file_path": "data/raw/CASE_009/DOC_001.pdf",
            "file_format": "pdf",
            "file_size_bytes": 1,
            "ocr_status": "not_applicable",
            "downstream_disposition": "expert_review_only",
        }],
    })

    rc = dao.cmd_read_document_text(make_args())

    out = capsys.readouterr().out
    assert rc == 1
    assert "NON_TEXT_EXPERT_REVIEW_ONLY" in out
    assert "redacted_text.md" not in out


# ------------------------------- T4a: in-process page-text read keeps gates --

def _cap_env(monkeypatch, tmp_path):
    monkeypatch.setattr(dao, "OUTPUTS", tmp_path / "outputs")
    monkeypatch.setattr(dao, "DATA", tmp_path / "data")
    page = dao.processed_dir("CASE_009", "DOC_001") / "page_001.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("환자 홍길동 010-1234-5678", encoding="utf-8")
    return page


def test_in_process_read_returns_text_with_a_live_capability(monkeypatch, tmp_path):
    _cap_env(monkeypatch, tmp_path)
    cap, path = dao._issue_page_text_capability("CASE_009", "DOC_001")
    try:
        text = dao.read_page_text_data("CASE_009", "DOC_001", 1,
                                       caller_stage="document-pipeline",
                                       capability=cap)
        assert "홍길동" in text
    finally:
        dao.release_page_text_capability(path)


def test_in_process_read_refuses_without_a_capability(monkeypatch, tmp_path):
    """Being in-process is not a reason to trust the caller. An importer is
    not more privileged than a subprocess."""
    _cap_env(monkeypatch, tmp_path)
    with pytest.raises(PermissionError) as exc:
        dao.read_page_text_data("CASE_009", "DOC_001", 1,
                                caller_stage="document-pipeline", capability="")
    assert "capability" in str(exc.value).lower()


def test_in_process_read_refuses_a_capability_minted_for_another_document(monkeypatch, tmp_path):
    _cap_env(monkeypatch, tmp_path)
    cap, path = dao._issue_page_text_capability("CASE_009", "DOC_002")
    try:
        with pytest.raises(PermissionError):
            dao.read_page_text_data("CASE_009", "DOC_001", 1,
                                    caller_stage="document-pipeline",
                                    capability=cap)
    finally:
        dao.release_page_text_capability(path)


def test_in_process_read_refuses_a_disallowed_caller_stage(monkeypatch, tmp_path):
    _cap_env(monkeypatch, tmp_path)
    cap, path = dao._issue_page_text_capability("CASE_009", "DOC_001")
    try:
        with pytest.raises(PermissionError) as exc:
            dao.read_page_text_data("CASE_009", "DOC_001", 1,
                                    caller_stage="denial-response",
                                    capability=cap)
        assert "not permitted" in str(exc.value)
    finally:
        dao.release_page_text_capability(path)


def test_in_process_read_refuses_after_the_capability_is_released(monkeypatch, tmp_path):
    """The window closes when checkpoint 2's finally runs."""
    _cap_env(monkeypatch, tmp_path)
    cap, path = dao._issue_page_text_capability("CASE_009", "DOC_001")
    dao.release_page_text_capability(path)
    with pytest.raises(PermissionError):
        dao.read_page_text_data("CASE_009", "DOC_001", 1,
                                caller_stage="document-pipeline", capability=cap)


def test_in_process_read_raises_rather_than_returning_a_denial_string(monkeypatch, tmp_path):
    """The CLI prints a denial and returns 1; a stdout-scraping caller could
    mistake that text for page content. The in-process form must raise."""
    _cap_env(monkeypatch, tmp_path)
    try:
        result = dao.read_page_text_data("CASE_009", "DOC_001", 1,
                                         caller_stage="evaluation", capability="x")
    except PermissionError:
        return
    pytest.fail(f"expected PermissionError, got a value: {result!r}")


# ------------------ institutional contacts in a PII-free class (2026-08-11) --

class TestInstitutionalContactExemption:
    """A published policy booklet carries its publisher's and the statutory
    notice's switchboards by construction. Blocking on those made every real
    policy document unprocessable while protecting nobody, and redacting them
    would corrupt the clause text a citation quotes.

    The exemption is narrow on purpose: it applies only when the caller has
    established a PII-free document class, it never runs on claim documents,
    and every other pattern still blocks. The tests that matter most are the
    ones proving a personal contact is NOT rescued by a nearby institution.
    """

    def test_statutory_credit_bureau_numbers_pass(self):
        # Real text from CASE_902 DOC_001 p4, wrapped exactly as the extractor
        # produces it -- the number spans newlines and its owner sits above.
        text = ("NICE \n신용정보\n( 주) : \n☎\n02\n- 2122\n- 4000  \n인터넷 www.nice.co.kr")
        assert rd.scan_residual_pii(text, allow_institutional_contacts=False),             "the strict scan must still see it"
        assert rd.scan_residual_pii(text) == []

    def test_publisher_mailbox_passes(self):
        text = "다이렉트 고객센터 Tel : 1577-3339 E-mail : mailmaster@samsungfire.com"
        assert rd.scan_residual_pii(text, allow_institutional_contacts=True) == []

    def test_representative_number_passes_without_a_named_institution(self):
        assert rd.scan_residual_pii("1588-5114", allow_institutional_contacts=True) == []

    # -- the disqualifiers: a nearby institution must not rescue a person ----

    def test_a_personal_mobile_is_not_rescued_by_a_nearby_institution(self):
        hits = rd.scan_residual_pii("서울대학교병원 담당 010-9876-5432",
                                    allow_institutional_contacts=True)
        assert any(h["kind"].startswith("phone_number") for h in hits), (
            "a claimant mobile beside a hospital name must still block")

    def test_a_consumer_email_is_not_rescued_by_a_nearby_institution(self):
        for addr in ("hong.gildong@gmail.com", "hong@naver.com", "x@daum.net"):
            hits = rd.scan_residual_pii(f"삼성화재 담당자 {addr}",
                                        allow_institutional_contacts=True)
            assert any(h["kind"] == "email" for h in hits), addr

    def test_a_bare_landline_with_no_institution_still_blocks(self):
        assert rd.scan_residual_pii("02-1234-5678", allow_institutional_contacts=True)

    @pytest.mark.parametrize("text,kind", [
        ("피보험자 800101-1234567", "resident_registration_number"),
        ("환급계좌 110-234-567890 신한은행", "account_number_dashed"),
        ("차량번호 12가3456", "vehicle_number"),
        ("일련 12345678901234", "long_digit_run"),
    ])
    def test_every_other_pattern_still_blocks_in_a_policy(self, text, kind):
        """The exemption must not become a blanket skip: a booklet that really
        did carry a claimant's RRN still fails closed."""
        hits = rd.scan_residual_pii(text, allow_institutional_contacts=True)
        assert any(h["kind"] == kind for h in hits)

    def test_exemptions_are_on_by_default_but_can_be_turned_off(self):
        """Default-on since 2026-08-11: an insurer's own footer appears in
        claim documents too (CASE_902 DOC_003, a 가입설계서, carried
        mailmaster@samsungfire.com on all 21 pages), so scoping the exemption
        to the policy class alone left every other document blocked on the same
        published contact. The strict scan stays reachable for a caller that
        wants it."""
        text = "고객센터 mailmaster@samsungfire.com"
        assert rd.scan_residual_pii(text) == []
        assert rd.scan_residual_pii(text, allow_institutional_contacts=False)

    def test_passthrough_redactor_uses_the_exemption(self):
        """The wiring, not just the helper: NoPiiClassRedactor is the only
        caller that may pass the flag."""
        text = "NICE 신용정보 (주) : 02 - 2122 - 4000"
        outcome = rd.NoPiiClassRedactor().redact_page(text)
        assert outcome.redacted_text == text
        assert outcome.items_redacted == 0

    def test_passthrough_redactor_still_blocks_real_pii(self):
        with pytest.raises(rd.RedactionLeakError):
            rd.NoPiiClassRedactor().redact_page("피보험자 홍길동 800101-1234567")
