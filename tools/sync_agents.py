"""Regenerates the Codex and generic-AGENTS.md copies of skills/agents from
the canonical .claude/ definitions.

See harness-guardrails-dev D3/D4. .claude/agents/*.md and
.claude/skills/*/SKILL.md are the single source of truth -- never hand-edit
.codex/agents/*.toml or .agents/skills/*/SKILL.md directly, since this
script will overwrite them. This is what makes the old drift (an .agents/
skill copy missing changelog entries the .claude/ version had) structurally
impossible going forward: the copies are build output, not maintained by
hand.

Skills copy verbatim (.claude/skills/{name}/SKILL.md -> .agents/skills/{name}/SKILL.md).
Agents get reformatted into TOML (.claude/agents/{name}.md -> .codex/agents/{name}.toml).

Usage:
    python tools/sync_agents.py
"""
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
CLAUDE_AGENTS = ROOT / ".claude" / "agents"
CLAUDE_SKILLS = ROOT / ".claude" / "skills"
CODEX_AGENTS = ROOT / ".codex" / "agents"
GENERIC_SKILLS = ROOT / ".agents" / "skills"

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        sys.exit("error: expected YAML frontmatter (--- ... ---) at the top of the file")
    fm_text, body = m.group(1), m.group(2)
    fields = {}
    for line in fm_text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields, body.strip()


def toml_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')


def sync_skills():
    GENERIC_SKILLS.mkdir(parents=True, exist_ok=True)
    count = 0
    for skill_dir in sorted(CLAUDE_SKILLS.iterdir()):
        skill_md = skill_dir / "SKILL.md"
        if not skill_dir.is_dir() or not skill_md.exists():
            continue
        dest_dir = GENERIC_SKILLS / skill_dir.name
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / "SKILL.md").write_text(skill_md.read_text(encoding="utf-8"), encoding="utf-8")
        count += 1
    print(f"OK: synced {count} skill(s) to {GENERIC_SKILLS}")


def sync_agents():
    CODEX_AGENTS.mkdir(parents=True, exist_ok=True)
    count = 0
    for agent_md in sorted(CLAUDE_AGENTS.glob("*.md")):
        fields, body = parse_frontmatter(agent_md.read_text(encoding="utf-8"))
        name = fields.get("name", agent_md.stem)
        description = fields.get("description", "")
        model = fields.get("model", "opus")
        toml_text = (
            f'name = "{toml_escape(name)}"\n'
            f'description = "{toml_escape(description)}"\n'
            f'model = "{toml_escape(model)}"\n\n'
            f'instructions = """\n{toml_escape(body)}\n"""\n'
        )
        (CODEX_AGENTS / f"{name}.toml").write_text(toml_text, encoding="utf-8")
        count += 1
    print(f"OK: synced {count} agent(s) to {CODEX_AGENTS}")


def main():
    sync_skills()
    sync_agents()


if __name__ == "__main__":
    main()
