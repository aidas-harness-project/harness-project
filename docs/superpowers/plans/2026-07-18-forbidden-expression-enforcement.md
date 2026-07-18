# Forbidden-Expression Content Enforcement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic checker that flags listed forbidden phrases in a rendered draft, consumed by the `critic` agent — mirroring the existing `read-evidence-tags` precedent.

**Architecture:** A read-only `tools/dao.py` subcommand `check-forbidden-expressions DOC_PATH` parses the "avoid" column of `templates/forbidden-expressions.md`, scans a draft (quote/whitespace-normalized), and prints JSON hits. The `critic` agent calls it and records hits as `forbidden_expression` findings plus an optional count field. No new agent, no write-time gate, record-only.

**Tech Stack:** Python 3 (stdlib only — `argparse`, `json`, `re`, `pathlib`), pytest, JSON Schema (draft 2020-12).

## Global Constraints

- **The DAO is the sole data-access path.** New checker is a `read-*`-family subcommand in `tools/dao.py`; do not create a standalone `tools/` script. (CLAUDE.md hard rule.)
- **`.claude/` is canonical.** After editing `.claude/agents/critic.md`, run `python tools/sync_agents.py` to regenerate `.codex/`/`.agents/` copies. Never hand-edit the generated copies.
- **Read-only, no lock, no state mutation.** The subcommand never writes, never acquires a lock.
- **Record-only.** A hit is recorded as a finding; it never forces `passed: false` and never halts the pipeline. `passed` stays the critic's judgment.
- **Not exhaustive.** `clean: true` means "no listed literal phrases present," NOT "no forbidden expressions." The semantic layer is the critic's existing P3 pass, left unchanged.
- **Authoritative source is `templates/forbidden-expressions.md`.** The checker reads that file and no other copy.
- **Every filesystem test runs against `tmp_path`**, never the real `outputs/`/`data/` trees. Tests import `dao` directly and call `cmd_*` functions via the `make_args`/`isolated_dao` fixtures in `tests/conftest.py`.
- **Additive schema change only.** The new field is optional (not added to `required`), so critic outputs written before this change still validate.

---

## Task 1: Normalization + phrase-loading helpers in dao.py

Pure functions, unit-tested in isolation. These are the only non-trivial logic; the command in Task 2 is thin glue over them.

**Files:**
- Modify: `tools/dao.py` (add a module constant near `OUTPUTS`/`DATA` at line 82–83; add two helper functions near `TAG_RE`/`cmd_read_evidence_tags` around line 480)
- Test: `tests/test_dao_forbidden_expr.py` (new)

**Interfaces:**
- Consumes: `ROOT` (module constant, `tools/dao.py:81`, = repo root `Path`).
- Produces:
  - `FORBIDDEN_TEMPLATE: Path` — `ROOT / "templates" / "forbidden-expressions.md"`.
  - `_normalize_expr(s: str) -> str` — straighten curly double-quotes, strip a single layer of surrounding double-quotes, collapse whitespace runs to one space, strip ends.
  - `_load_forbidden_phrases(template_path: Path) -> list[str]` — return the normalized first-column ("위험 표현") phrases from the markdown table. Returns `[]` if no parseable table. Raises `FileNotFoundError` if the file does not exist.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_dao_forbidden_expr.py`:

```python
"""dao.py's check-forbidden-expressions -- the deterministic floor critic.md
uses for the listed literal forbidden phrases (templates/forbidden-expressions.md).
The semantic/implied cases stay with critic's P3 pass; this tool is the floor,
not full coverage."""
import json

import dao


# ---- Task 1: helpers ----

def test_normalize_strips_wrapping_quotes_and_collapses_space():
    assert dao._normalize_expr('  "보험사는   반드시  지급해야 한다"  ') == "보험사는 반드시 지급해야 한다"


def test_normalize_maps_curly_quotes_to_straight():
    # A curly-quoted phrase and a straight-quoted phrase normalize identically.
    curly = dao._normalize_expr('“의학적으로 명백하다”')
    straight = dao._normalize_expr('"의학적으로 명백하다"')
    assert curly == straight == "의학적으로 명백하다"


def test_load_forbidden_phrases_reads_avoid_column(tmp_path):
    md = tmp_path / "forbidden-expressions.md"
    md.write_text(
        "# 표현 대조표\n\n"
        "| 위험 표현 | 대체 표현 |\n"
        "| --- | --- |\n"
        '| "보험사는 반드시 지급해야 한다" | "지급 가능성을 검토할 여지가 있다" |\n'
        '| "의학적으로 명백하다" | "의무기록상 확인이 필요하다" |\n',
        encoding="utf-8",
    )
    phrases = dao._load_forbidden_phrases(md)
    assert "보험사는 반드시 지급해야 한다" in phrases
    assert "의학적으로 명백하다" in phrases
    # Header row and separator row must not leak in as phrases.
    assert "위험 표현" not in phrases
    assert all("---" not in p for p in phrases)


def test_load_forbidden_phrases_no_table_returns_empty(tmp_path):
    md = tmp_path / "forbidden-expressions.md"
    md.write_text("# 배경\n\n표가 없는 문서.\n", encoding="utf-8")
    assert dao._load_forbidden_phrases(md) == []


def test_load_forbidden_phrases_missing_file_raises(tmp_path):
    import pytest
    with pytest.raises(FileNotFoundError):
        dao._load_forbidden_phrases(tmp_path / "nope.md")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/toxiclemon/Working/Labs/AIDAS/harness-project && python -m pytest tests/test_dao_forbidden_expr.py -v`
Expected: FAIL — `AttributeError: module 'dao' has no attribute '_normalize_expr'`.

- [ ] **Step 3: Add the module constant**

In `tools/dao.py`, directly after the existing lines (81–83):

```python
ROOT = Path(__file__).resolve().parent.parent
OUTPUTS = ROOT / "outputs"
DATA = ROOT / "data"
```

add:

```python
FORBIDDEN_TEMPLATE = ROOT / "templates" / "forbidden-expressions.md"
```

- [ ] **Step 4: Add the helper functions**

In `tools/dao.py`, immediately before `def cmd_read_evidence_tags(args):` (currently line 483), add:

```python
_MD_TABLE_ROW = re.compile(r"^\|(.+)\|$")


def _normalize_expr(s: str) -> str:
    """Normalize a forbidden-expression phrase or a draft line for matching:
    straighten curly double-quotes, strip one layer of surrounding double-quotes,
    collapse internal whitespace runs to a single space. Deliberately literal --
    this is a floor, not a paraphrase detector."""
    s = s.replace("“", '"').replace("”", '"')
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1].strip()
    return s


def _load_forbidden_phrases(template_path: Path) -> list[str]:
    """Return the normalized first-column ('위험 표현') phrases from the markdown
    table in templates/forbidden-expressions.md. Returns [] if no parseable table
    (caller treats that as a setup failure, never a clean draft). Raises
    FileNotFoundError if the file is absent."""
    text = template_path.read_text(encoding="utf-8")  # raises FileNotFoundError if absent
    phrases = []
    for line in text.splitlines():
        m = _MD_TABLE_ROW.match(line.strip())
        if not m:
            continue
        cells = [c.strip() for c in m.group(1).split("|")]
        if not cells:
            continue
        first = cells[0]
        # Skip the header row and the |---|---| separator row.
        if first in ("위험 표현", "") or set(first) <= set("-: "):
            continue
        norm = _normalize_expr(first)
        if norm:
            phrases.append(norm)
    return phrases
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /home/toxiclemon/Working/Labs/AIDAS/harness-project && python -m pytest tests/test_dao_forbidden_expr.py -v`
Expected: PASS (5 tests).

- [ ] **Step 6: Commit**

```bash
cd /home/toxiclemon/Working/Labs/AIDAS/harness-project
git add tools/dao.py tests/test_dao_forbidden_expr.py
git commit -m "Add forbidden-expression parse/normalize helpers to dao.py

Pure helpers for the deterministic forbidden-expression floor: quote/whitespace
normalization and markdown-table avoid-column parsing against
templates/forbidden-expressions.md.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: The `check-forbidden-expressions` subcommand

Thin glue over Task 1's helpers: the `cmd_*` function, subparser wiring, and docstring entry. Tested via the same `make_args`/`isolated_dao` pattern as `read-evidence-tags`.

**Files:**
- Modify: `tools/dao.py` (add `cmd_check_forbidden_expressions` after the helpers from Task 1; add subparser after the `read-evidence-tags` block at lines 857–858; add a docstring line after the `read-evidence-tags DOC_PATH` entry at line ~62)
- Test: `tests/test_dao_forbidden_expr.py` (extend)

**Interfaces:**
- Consumes: `_normalize_expr`, `_load_forbidden_phrases`, `FORBIDDEN_TEMPLATE` (Task 1); `make_args`, `isolated_dao` (`tests/conftest.py` — `make_args` already defaults a `doc_path=None` attribute, so the new subcommand reuses the arg name `doc_path` and needs no conftest change).
- Produces: `cmd_check_forbidden_expressions(args) -> int` — reads `args.doc_path` (the draft). Prints JSON `{"clean": bool, "hits": [{"phrase": str, "line": int|null}], "source": str, "note": str}`. Exit contract: `0` clean, `1` on hit or missing draft (`NOT_FOUND: <path>`), `2` on missing/empty template (`NO_TEMPLATE: <path>`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_dao_forbidden_expr.py`:

```python
# ---- Task 2: the subcommand ----

def _write_draft(root, text):
    p = root / "outputs" / "CASE_009" / "draft_report_v1.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_listed_phrase_is_a_hit(isolated_dao, make_args, capsys):
    draft = _write_draft(isolated_dao, "결론적으로 보험사는 반드시 지급해야 한다.\n")
    rc = dao.cmd_check_forbidden_expressions(make_args(doc_path=str(draft)))
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["clean"] is False
    assert out["hits"][0]["phrase"] == "보험사는 반드시 지급해야 한다"
    assert out["hits"][0]["line"] == 1


def test_clean_draft_is_clean(isolated_dao, make_args, capsys):
    draft = _write_draft(isolated_dao, "지급 가능성을 검토할 여지가 있다.\n")
    rc = dao.cmd_check_forbidden_expressions(make_args(doc_path=str(draft)))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["clean"] is True
    assert out["hits"] == []


def test_curly_quote_variant_still_a_hit(isolated_dao, make_args, capsys):
    # The template is straight-quoted; a draft using curly quotes must still hit.
    draft = _write_draft(isolated_dao, "“의학적으로 명백하다” 라고 볼 수 있다.\n")
    rc = dao.cmd_check_forbidden_expressions(make_args(doc_path=str(draft)))
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert any(h["phrase"] == "의학적으로 명백하다" for h in out["hits"])


def test_internal_whitespace_variant_still_a_hit(isolated_dao, make_args, capsys):
    draft = _write_draft(isolated_dao, "보험사는  반드시   지급해야 한다 고 판단된다.\n")
    rc = dao.cmd_check_forbidden_expressions(make_args(doc_path=str(draft)))
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert any(h["phrase"] == "보험사는 반드시 지급해야 한다" for h in out["hits"])


def test_near_miss_paraphrase_is_not_a_literal_hit(isolated_dao, make_args, capsys):
    # Proves the floor's boundary honestly: paraphrase is the critic's job, not this tool's.
    draft = _write_draft(isolated_dao, "당연히 지급되어야 마땅하다.\n")
    rc = dao.cmd_check_forbidden_expressions(make_args(doc_path=str(draft)))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["clean"] is True


def test_missing_draft_reports_not_found(isolated_dao, make_args, capsys):
    rc = dao.cmd_check_forbidden_expressions(make_args(doc_path=str(isolated_dao / "nope.md")))
    assert rc == 1
    assert "NOT_FOUND" in capsys.readouterr().out


def test_missing_template_reports_no_template(isolated_dao, make_args, capsys, monkeypatch):
    draft = _write_draft(isolated_dao, "보험사는 반드시 지급해야 한다.\n")
    monkeypatch.setattr(dao, "FORBIDDEN_TEMPLATE", isolated_dao / "absent.md")
    rc = dao.cmd_check_forbidden_expressions(make_args(doc_path=str(draft)))
    assert rc == 2
    assert "NO_TEMPLATE" in capsys.readouterr().out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/toxiclemon/Working/Labs/AIDAS/harness-project && python -m pytest tests/test_dao_forbidden_expr.py -k "hit or clean or template or found or paraphrase" -v`
Expected: FAIL — `AttributeError: module 'dao' has no attribute 'cmd_check_forbidden_expressions'`.

- [ ] **Step 3: Add the command function**

In `tools/dao.py`, immediately after `_load_forbidden_phrases` (from Task 1) and before `def cmd_read_evidence_tags(args):`, add:

```python
def cmd_check_forbidden_expressions(args):
    """Deterministic floor: scan a rendered draft for the listed literal phrases
    in templates/forbidden-expressions.md. Record-only -- the critic decides
    passed. Not exhaustive; the semantic/implied cases are the critic's P3 pass."""
    draft_path = Path(args.doc_path)
    try:
        phrases = _load_forbidden_phrases(FORBIDDEN_TEMPLATE)
    except FileNotFoundError:
        print(f"NO_TEMPLATE: {FORBIDDEN_TEMPLATE}")
        return 2
    if not phrases:
        print(f"NO_TEMPLATE: {FORBIDDEN_TEMPLATE}")
        return 2
    if not draft_path.exists():
        print(f"NOT_FOUND: {draft_path}")
        return 1

    raw_lines = draft_path.read_text(encoding="utf-8").splitlines()
    norm_lines = [_normalize_expr(ln) for ln in raw_lines]
    norm_full = _normalize_expr(" ".join(raw_lines))

    hits = []
    for phrase in phrases:
        line_no = None
        for i, nl in enumerate(norm_lines, start=1):
            if phrase in nl:
                line_no = i
                break
        if line_no is None and phrase in norm_full:
            line_no = None  # present but spans soft-wrapped lines
        elif line_no is None:
            continue
        hits.append({"phrase": phrase, "line": line_no})

    clean = not hits
    print(json.dumps({
        "clean": clean,
        "hits": hits,
        "source": "templates/forbidden-expressions.md",
        "note": "listed literal phrases only; not exhaustive -- semantic P3 coverage is the critic's",
    }, ensure_ascii=False))
    return 0 if clean else 1
```

- [ ] **Step 4: Wire the subparser**

In `tools/dao.py`, immediately after the `read-evidence-tags` block (lines 857–858):

```python
    p = sub.add_parser("read-evidence-tags"); p.add_argument("doc_path")
    p.set_defaults(fn=cmd_read_evidence_tags)
```

add:

```python
    p = sub.add_parser("check-forbidden-expressions"); p.add_argument("doc_path")
    p.set_defaults(fn=cmd_check_forbidden_expressions)
```

- [ ] **Step 5: Add the docstring entry**

In `tools/dao.py`, in the module docstring's Subcommands list, immediately after the line:

```
    read-evidence-tags DOC_PATH
```

add:

```
    check-forbidden-expressions DOC_PATH
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd /home/toxiclemon/Working/Labs/AIDAS/harness-project && python -m pytest tests/test_dao_forbidden_expr.py -v`
Expected: PASS (12 tests total).

- [ ] **Step 7: Verify the real CLI end-to-end against the real template**

Run:
```bash
cd /home/toxiclemon/Working/Labs/AIDAS/harness-project
printf '결론적으로 보험사는 반드시 지급해야 한다.\n' > /tmp/fe_draft.md
python tools/dao.py check-forbidden-expressions /tmp/fe_draft.md; echo "exit=$?"
rm -f /tmp/fe_draft.md
```
Expected: JSON with `"clean": false` and a hit on `보험사는 반드시 지급해야 한다`, then `exit=1`. Confirms the subcommand resolves the real `templates/forbidden-expressions.md` (not a fixture) and parses its real table.

- [ ] **Step 8: Commit**

```bash
cd /home/toxiclemon/Working/Labs/AIDAS/harness-project
git add tools/dao.py tests/test_dao_forbidden_expr.py
git commit -m "Add dao.py check-forbidden-expressions subcommand

Read-only floor mirroring read-evidence-tags: scans a rendered draft for the
listed literal phrases in templates/forbidden-expressions.md, prints JSON hits,
exit 0 clean / 1 hit-or-missing-draft / 2 missing-or-empty-template. Record-only,
no lock, no write. Not exhaustive -- semantic coverage stays the critic's P3 pass.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Optional `forbidden_literal_hit_count` schema field

**Files:**
- Modify: `schemas/critic_result.schema.json` (add a property after `unused_citation_count` at lines 24–28; bump the title version; do NOT touch `required`)
- Test: `tests/test_dao_forbidden_expr.py` (extend with a schema backward-compat test)

**Interfaces:**
- Consumes: `load_registry` and `validate_instance` from `_validation` (`tools/_validation.py:17,68`) — `load_registry() -> (schemas, registry)`, `validate_instance(instance, schema_name, schemas, registry) -> list[str]` (empty list = valid).
- Produces: schema `critic_result.schema.json` accepting an optional integer `forbidden_literal_hit_count`, while still accepting instances that omit it.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_dao_forbidden_expr.py`:

```python
# ---- Task 3: schema backward-compat + new field ----

def _minimal_critic_result(extra=None):
    inst = {
        "case_id": "CASE_009",
        "component": "critic",
        "status": "success",
        "reviewed_document": "outputs/CASE_009/draft_report_v1.md",
        "passed": True,
        "orphaned_tag_count": 0,
        "unused_citation_count": 0,
        "findings": [],
    }
    if extra:
        inst.update(extra)
    return inst


def test_critic_result_without_new_field_still_validates():
    from _validation import load_registry, validate_instance
    schemas, registry = load_registry()
    inst = _minimal_critic_result()  # no forbidden_literal_hit_count -- pre-2026-07-18 shape
    assert validate_instance(inst, "critic_result.schema.json", schemas, registry) == []


def test_critic_result_accepts_forbidden_literal_hit_count():
    from _validation import load_registry, validate_instance
    schemas, registry = load_registry()
    inst = _minimal_critic_result({"forbidden_literal_hit_count": 2})
    assert validate_instance(inst, "critic_result.schema.json", schemas, registry) == []


def test_critic_result_rejects_negative_hit_count():
    from _validation import load_registry, validate_instance
    schemas, registry = load_registry()
    inst = _minimal_critic_result({"forbidden_literal_hit_count": -1})
    assert validate_instance(inst, "critic_result.schema.json", schemas, registry) != []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/toxiclemon/Working/Labs/AIDAS/harness-project && python -m pytest tests/test_dao_forbidden_expr.py -k "critic_result" -v`
Expected: `test_critic_result_rejects_negative_hit_count` FAILS (schema currently ignores the unknown field, so a `-1` value is accepted → `validate_instance` returns `[]`, assertion `!= []` fails). The other two pass trivially — that's fine; the negative test is the one proving the field is really constrained.

- [ ] **Step 3: Add the schema property**

In `schemas/critic_result.schema.json`, the block at lines 24–28 is:

```json
        "unused_citation_count": {
          "type": "integer",
          "minimum": 0,
          "description": "Sidecar entries never referenced by a tag in the document."
        },
```

Immediately after it (before `"findings"`), add:

```json
        "forbidden_literal_hit_count": {
          "type": "integer",
          "minimum": 0,
          "description": "Count of listed literal forbidden phrases found in the draft by dao.py check-forbidden-expressions. Optional/additive (deliberately NOT in required) -- critic outputs written before 2026-07-18 legitimately omit it. Floor only: the listed literal phrases, not the semantic P3 pass."
        },
```

Leave the `"required"` array unchanged. Bump the title on line 4 from `v0.1` to `v0.2`:

```json
  "title": "critic_result.json -- blind draft review findings v0.2",
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/toxiclemon/Working/Labs/AIDAS/harness-project && python -m pytest tests/test_dao_forbidden_expr.py -k "critic_result" -v`
Expected: PASS (3 tests). The negative-value case is now rejected; the no-field case still validates.

- [ ] **Step 5: Commit**

```bash
cd /home/toxiclemon/Working/Labs/AIDAS/harness-project
git add schemas/critic_result.schema.json tests/test_dao_forbidden_expr.py
git commit -m "critic_result: optional forbidden_literal_hit_count (v0.2)

Additive integer>=0 field for the deterministic forbidden-expression floor.
Deliberately not in required -- committed CASE_021/022 critic outputs predate it
and must keep validating. Backward-compat test included.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Wire the critic agent + changelog

Documentation/agent-spec change. No unit test; verification is the sync round-trip and the full suite staying green.

**Files:**
- Modify: `.claude/agents/critic.md` (the forbidden-expressions bullet at line 19; the `# Output` paragraph that lists recorded counts)
- Regenerate (do not hand-edit): `.codex/agents/critic.toml`, `.agents/skills/*` — via `tools/sync_agents.py`
- Modify: `CLAUDE.md` (add a changelog row before the "Open decisions deferred for later" line)

**Interfaces:**
- Consumes: the `check-forbidden-expressions` subcommand (Task 2) and the `forbidden_literal_hit_count` field (Task 3).
- Produces: nothing code depends on — this task makes the agent actually call the tool.

- [ ] **Step 1: Update the forbidden-expressions bullet**

In `.claude/agents/critic.md`, the line (19) currently reads:

```
- **Forbidden expressions:** scan for definitive legal/medical language that should have been substituted (see `templates/forbidden-expressions.md`). Check against that file, not `pipeline.md`'s copy of the table -- `templates/` is the authoritative set per `open-decisions.md` #2, and it's what `draft-report` is told to write against. Checking a different copy than the writer follows is how the two drift apart unnoticed.
```

Replace it with:

```
- **Forbidden expressions:** run `python tools/dao.py check-forbidden-expressions <draft_path>` for the deterministic floor -- it flags every *listed literal* phrase from `templates/forbidden-expressions.md` (the authoritative set per `open-decisions.md` #2, and what `draft-report` writes against). Turn each returned hit into a `forbidden_expression` finding, and record the count in `forbidden_literal_hit_count`. Then still do your own semantic pass on top for the *implied* cases the literal floor cannot see -- unhedged paraphrases of the same assertions (e.g. "당연히 지급되어야 마땅하다"). A `clean: true` from the tool does NOT discharge that semantic pass; both run on every version. Record-only: a literal hit is a finding, not an automatic `passed: false` -- you still set `passed` on judgment.
```

- [ ] **Step 2: Update the Output paragraph to mention the count**

In `.claude/agents/critic.md`, in the `# Output` section, find the sentence describing recorded counts (it currently names `orphaned_tag_count`/`unused_citation_count`). Append to that same sentence, after `unused_citation_count`:

Change:

```
record the counts in `orphaned_tag_count`/`unused_citation_count`, not just as findings.
```

to:

```
record the counts in `orphaned_tag_count`/`unused_citation_count`/`forbidden_literal_hit_count`, not just as findings.
```

(This phrase appears in the "What you check" fabrication bullet at line 17; make the same one-word-list edit wherever the count fields are enumerated. If it appears only once, edit that one occurrence.)

- [ ] **Step 3: Regenerate the Codex/generic copies**

Run:
```bash
cd /home/toxiclemon/Working/Labs/AIDAS/harness-project
python tools/sync_agents.py
```
Expected: `OK: synced 3 skill(s)...` and `OK: synced 10 agent(s)...`.

- [ ] **Step 4: Verify the reference propagated and no stale copy remains**

Run:
```bash
cd /home/toxiclemon/Working/Labs/AIDAS/harness-project
grep -c 'check-forbidden-expressions' .codex/agents/critic.toml
grep -rn "pipeline.md'\''s copy of the table" .claude/ .codex/ .agents/ || echo "no stale pipeline.md-copy reference"
```
Expected: first command prints `1` (or more); second prints the "no stale" line.

- [ ] **Step 5: Run the full suite**

Run: `cd /home/toxiclemon/Working/Labs/AIDAS/harness-project && python -m pytest -q`
Expected: all tests pass (245 prior + the new `test_dao_forbidden_expr.py` cases). The `test_sync_agents.py` suite confirms the generated copies match the canonical `.claude/` source.

- [ ] **Step 6: Add the CLAUDE.md changelog row**

In `CLAUDE.md`, immediately before the line beginning `**Open decisions deferred for later:**`, insert:

```
| 2026-07-18 | Built the forbidden-expression deterministic floor (spec: `docs/superpowers/specs/2026-07-18-forbidden-expression-enforcement-design.md`). New read-only `dao.py check-forbidden-expressions DOC_PATH` scans a rendered draft for the *listed literal* phrases in `templates/forbidden-expressions.md` and prints JSON hits (exit 0 clean / 1 hit-or-missing-draft / 2 missing-or-empty-template) -- mirroring `read-evidence-tags`. `critic` now calls it, records hits as `forbidden_expression` findings + a new optional `forbidden_literal_hit_count` (critic_result v0.2, additive -- pre-2026-07-18 outputs still validate), and still runs its semantic P3 pass for the implied/paraphrase cases the literal floor can't see. Record-only (a hit is a finding, never an auto-`passed:false`), no write-time gate (findings-not-halt preserved), no new agent (the critic already owns the semantic layer). Quote/whitespace normalization handles the straight-vs-curly drift found on 2026-07-17. New `tests/test_dao_forbidden_expr.py`. | tools/dao.py, schemas/critic_result, agents/critic, tests/ | See the design spec |
```

- [ ] **Step 7: Commit**

```bash
cd /home/toxiclemon/Working/Labs/AIDAS/harness-project
git add .claude/agents/critic.md .codex/agents/critic.toml .agents/ CLAUDE.md
git commit -m "Wire critic to check-forbidden-expressions floor

critic now runs the deterministic checker, records hits as forbidden_expression
findings + forbidden_literal_hit_count, and still does its semantic P3 pass for
the implied cases. Record-only. Generated .codex/.agents copies regenerated via
sync_agents.py; changelog row added.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage** (against `2026-07-18-forbidden-expression-enforcement-design.md`):
- Checker subcommand `check-forbidden-expressions` → Task 2. ✓
- Parse avoid column of `templates/forbidden-expressions.md` → Task 1 `_load_forbidden_phrases`. ✓
- Quote/whitespace normalization → Task 1 `_normalize_expr` + tests. ✓
- JSON output `{clean, hits, source, note}`, exit 0/1/2 → Task 2. ✓
- `NOT_FOUND` / `NO_TEMPLATE` distinction → Task 2 command + tests. ✓
- Critic consumes it, hits → findings, records count → Task 4. ✓
- Semantic pass unchanged, both run every version → Task 4 bullet wording. ✓
- Optional `forbidden_literal_hit_count`, not required, version bump → Task 3. ✓
- Backward-compat (pre-field instance validates) → Task 3 test. ✓
- Honesty boundary (`clean` ≠ exhaustive) → Task 2 `note` field + Task 4 wording. ✓
- Record-only, no auto-fail, no write-gate, no new agent → Global Constraints + Task 4. ✓
- Tests mirror `test_dao_evidence_tags.py` incl. near-miss paraphrase → Task 2. ✓
- `document_assembly.py` untouched → not in any task's Files. ✓
- Deferred (writer self-check, consolidation-guard test) → correctly absent from all tasks. ✓

**Placeholder scan:** No TBD/TODO; every code step shows complete code; every command shows expected output. ✓

**Type consistency:** `_normalize_expr(str)->str`, `_load_forbidden_phrases(Path)->list[str]`, `cmd_check_forbidden_expressions(args)->int`, arg name `doc_path` (matches the conftest `make_args` default), field `forbidden_literal_hit_count` — used identically across Tasks 1–4. `load_registry()->(schemas, registry)` and `validate_instance(instance, name, schemas, registry)->list` match `tools/_validation.py`. ✓
