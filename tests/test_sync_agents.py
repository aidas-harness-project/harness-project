"""sync_agents.py -- regenerates .codex/agents/*.toml and
.agents/skills/*/SKILL.md from the canonical .claude/ definitions
(harness-guardrails-dev D3/D4). Previously untested (known-gaps.md item
4's residual) despite being what keeps the three copies from drifting --
exercised here against tmp_path fixture trees, never the real repo's
.claude/.codex/.agents directories.
"""
from types import SimpleNamespace

import pytest

import sync_agents as sa


@pytest.fixture
def isolated_sync(tmp_path, monkeypatch):
    claude_agents = tmp_path / ".claude" / "agents"
    claude_skills = tmp_path / ".claude" / "skills"
    codex_agents = tmp_path / ".codex" / "agents"
    generic_skills = tmp_path / ".agents" / "skills"
    claude_agents.mkdir(parents=True)
    claude_skills.mkdir(parents=True)
    monkeypatch.setattr(sa, "CLAUDE_AGENTS", claude_agents)
    monkeypatch.setattr(sa, "CLAUDE_SKILLS", claude_skills)
    monkeypatch.setattr(sa, "CODEX_AGENTS", codex_agents)
    monkeypatch.setattr(sa, "GENERIC_SKILLS", generic_skills)
    return SimpleNamespace(claude_agents=claude_agents, claude_skills=claude_skills,
                            codex_agents=codex_agents, generic_skills=generic_skills)


# ------------------------------------------------------- parse_frontmatter --

def test_parse_frontmatter_extracts_fields_and_body():
    text = "---\nname: foo\ndescription: does a thing\nmodel: opus\n---\nThe actual body text."

    fields, body = sa.parse_frontmatter(text)

    assert fields == {"name": "foo", "description": "does a thing", "model": "opus"}
    assert body == "The actual body text."


def test_parse_frontmatter_missing_block_exits():
    with pytest.raises(SystemExit):
        sa.parse_frontmatter("No frontmatter here, just body text.")


def test_parse_frontmatter_ignores_lines_without_a_colon():
    text = "---\nname: foo\nnot a field line\ndescription: bar\n---\nbody"

    fields, _ = sa.parse_frontmatter(text)

    assert fields == {"name": "foo", "description": "bar"}


# -------------------------------------------------------------- toml_escape --

def test_toml_escape_backslashes():
    assert sa.toml_escape(r"a\b") == r"a\\b"


def test_toml_escape_triple_quotes():
    assert sa.toml_escape('has """ inside') == 'has \\"\\"\\" inside'


# -------------------------------------------------------------- sync_skills --

def test_sync_skills_copies_content_verbatim(isolated_sync):
    skill_dir = isolated_sync.claude_skills / "my-skill"
    skill_dir.mkdir()
    content = "---\nname: my-skill\ndescription: test\n---\nSkill body with 한글 and \"quotes\"."
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")

    sa.sync_skills()

    dest = isolated_sync.generic_skills / "my-skill" / "SKILL.md"
    assert dest.read_text(encoding="utf-8") == content


def test_sync_skills_skips_directories_without_a_skill_md(isolated_sync):
    (isolated_sync.claude_skills / "not-a-skill").mkdir()  # no SKILL.md inside

    sa.sync_skills()

    assert not (isolated_sync.generic_skills / "not-a-skill").exists()


def test_sync_skills_ignores_stray_files_at_the_top_level(isolated_sync):
    (isolated_sync.claude_skills / "README.md").write_text("not a skill dir", encoding="utf-8")

    sa.sync_skills()  # must not crash on a non-directory entry

    assert not (isolated_sync.generic_skills / "README.md").exists()


# -------------------------------------------------------------- sync_agents --

def test_sync_agents_writes_toml_with_expected_fields(isolated_sync):
    (isolated_sync.claude_agents / "my-agent.md").write_text(
        "---\nname: my-agent\ndescription: does things\nmodel: sonnet\n---\nBody text here.",
        encoding="utf-8",
    )

    sa.sync_agents()

    toml_text = (isolated_sync.codex_agents / "my-agent.toml").read_text(encoding="utf-8")
    assert 'name = "my-agent"' in toml_text
    assert 'description = "does things"' in toml_text
    assert 'model = "sonnet"' in toml_text
    assert 'developer_instructions = """' in toml_text
    assert "Body text here." in toml_text


def test_sync_agents_defaults_model_to_opus_when_absent(isolated_sync):
    (isolated_sync.claude_agents / "no-model-agent.md").write_text(
        "---\nname: no-model-agent\ndescription: x\n---\nbody", encoding="utf-8",
    )

    sa.sync_agents()

    toml_text = (isolated_sync.codex_agents / "no-model-agent.toml").read_text(encoding="utf-8")
    assert 'model = "opus"' in toml_text


def test_sync_agents_escapes_triple_quotes_in_body(isolated_sync):
    """A body containing ''' \"\"\" would otherwise break out of the TOML
    triple-quoted instructions string -- must be escaped, not passed through."""
    (isolated_sync.claude_agents / "quoted-agent.md").write_text(
        '---\nname: quoted-agent\ndescription: x\n---\nBody with a literal """ triple quote inside.',
        encoding="utf-8",
    )

    sa.sync_agents()

    toml_text = (isolated_sync.codex_agents / "quoted-agent.toml").read_text(encoding="utf-8")
    assert '\\"\\"\\"' in toml_text
    # The developer-instructions block's own delimiters must still be exactly two: open and close
    assert toml_text.count('developer_instructions = """') == 1


def test_sync_agents_processes_every_md_file_in_the_directory(isolated_sync):
    for i in range(3):
        (isolated_sync.claude_agents / f"agent-{i}.md").write_text(
            f"---\nname: agent-{i}\ndescription: x\n---\nbody {i}", encoding="utf-8",
        )

    sa.sync_agents()

    assert sorted(p.name for p in isolated_sync.codex_agents.glob("*.toml")) == \
        ["agent-0.toml", "agent-1.toml", "agent-2.toml"]


def test_sync_agents_missing_frontmatter_in_one_file_halts_before_finishing(isolated_sync):
    (isolated_sync.claude_agents / "a-good.md").write_text(
        "---\nname: a-good\ndescription: x\n---\nbody", encoding="utf-8",
    )
    (isolated_sync.claude_agents / "b-bad.md").write_text("no frontmatter at all", encoding="utf-8")

    with pytest.raises(SystemExit):
        sa.sync_agents()


# ------------------------------------------------------------------- main() --

def test_main_runs_both_syncs_without_error(isolated_sync, capsys):
    (isolated_sync.claude_agents / "agent.md").write_text(
        "---\nname: agent\ndescription: x\n---\nbody", encoding="utf-8",
    )
    skill_dir = isolated_sync.claude_skills / "skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: skill\ndescription: x\n---\nbody", encoding="utf-8")

    sa.main()

    out = capsys.readouterr().out
    assert "synced 1 skill" in out
    assert "synced 1 agent" in out
    assert (isolated_sync.codex_agents / "agent.toml").exists()
    assert (isolated_sync.generic_skills / "skill" / "SKILL.md").exists()
