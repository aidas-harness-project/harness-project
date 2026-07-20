# Forbidden-Expression Content Enforcement -- Design Spec

## Purpose

Give the forbidden-expression list a deterministic enforcement floor, the way the
R01--R99 taxonomy has one (a schema `enum` validated at DAO write time) and the way
`[E#]` citations have one (`dao.py read-evidence-tags`).

Today the list in `templates/forbidden-expressions.md` is enforced only by the
`critic` agent *reading the table and judging by eye*. Nothing mechanical checks a
rendered draft against it. `tools/document_assembly.py` does zero content scanning
(verified). So a listed phrase in a draft rides entirely on the critic model
noticing it -- there is no floor beneath that attention.

This spec adds that floor: a read-only checker the critic consumes, mirroring
`read-evidence-tags` one-for-one. It does **not** add an agent, and it does **not**
touch the semantic layer -- see Non-Goals.

## Background / why this shape

Established two-layer pattern in this repo, which this change follows rather than
invents:

| Concern | Deterministic layer | Semantic layer |
|---|---|---|
| Denial codes | schema `enum`, rejected at write | (none needed -- codes are discrete) |
| `[E#]` citations | `read-evidence-tags` (orphaned/unused) | critic judges whether the cited evidence actually supports the claim |
| OCR reads | `compare()` fact/addition diff | human image review on disagreement |
| **Forbidden expressions** | **MISSING -- this spec** | critic's P3 pass (already exists) |

The semantic/"implied" layer for forbidden expressions is **already the critic**.
`critic.md`'s P3 check ("every inference is hedged and flagged, not asserted
outright") is exactly what catches an unlisted paraphrase like
`당연히 지급되어야 마땅하다`. A new agent for that would duplicate the critic, which
the 10-agent redesign deliberately split apart to *remove* bundled identities. So
the only missing piece is the deterministic floor for the *listed literal* phrases.

Hard constraint that rules out the obvious alternative: `critic.md`'s error-handling
states a prohibited-expression issue is "your normal output (a finding), not a stage
failure -- record it, don't halt." A write-refusing gate in `document_assembly.py`
would fight this -- it would either halt wrongly or write nothing and starve the
audit trail. The deterministic layer must therefore be a **detection the critic
consumes**, never a write-time gate.

## Scope

In scope:
- A read-only DAO subcommand `check-forbidden-expressions DRAFT_PATH` that scans a
  rendered draft against `templates/forbidden-expressions.md` and returns literal
  hits.
- Wiring `critic` to call it, turn each hit into a `forbidden_expression` finding,
  and record a mechanical `forbidden_literal_hit_count`.
- One additive, **optional** field on `critic_result.schema.json`.
- Tests mirroring `tests/test_dao_evidence_tags.py`.

Out of scope (Non-Goals):
- **No new agent.** The implied/paraphrase cases stay with the critic's existing P3
  semantic pass, unchanged.
- **No write-time gate.** `document_assembly.py` is not modified; nothing refuses a
  write on a hit.
- **No auto-fail.** A literal hit is recorded as a finding; whether it flips
  `passed: false` remains the critic's judgment (record-only, per decision). The
  deterministic result never overrides critic discretion.
- **No completeness claim.** The table is illustrative, not exhaustive. `clean:
  true` means "none of the listed literal phrases present," not "no forbidden
  expressions." That honesty boundary is stated in the tool output help and the
  critic instruction, so a green check is never misread as full coverage.
- **No `draft-report` self-check** in this pass. The critic is the gate, mirroring
  the evidence-tag precedent (critic-only). A writer-side self-check is a possible
  later add, noted under Future.

## Architecture

### 1. The checker: `dao.py check-forbidden-expressions DRAFT_PATH`

Placement: a `read-*`-family subcommand in `tools/dao.py`, alongside
`read-evidence-tags`. Read-only, no lock, pure function. Chosen over a standalone
`tools/` script because the analogous check already lives in the DAO and CLAUDE.md
names the DAO "the sole data-access path"; over folding into `document_assembly.py`
because the consumer is the critic, not the assembler.

Behavior:
1. Load the authoritative table from `templates/forbidden-expressions.md`. Parse the
   markdown table's first ("avoid") column. This read is what makes `templates/` the
   machine-consumed source of truth -- closing the loop started by the 2026-07-17
   consolidation commit.
2. Load the draft at `DRAFT_PATH`.
3. For each avoid-phrase, search the draft (normalized -- see below). Collect hits
   with their 1-indexed line number.
4. Print JSON: `{"clean": bool, "hits": [{"phrase": str, "line": int}], "source": "templates/forbidden-expressions.md", "note": "listed literal phrases only; not exhaustive -- semantic P3 coverage is the critic's"}`.
5. Exit 0 if clean, 1 if any hit (mirrors `read-evidence-tags`' exit contract).
6. Missing draft -> print `NOT_FOUND: <path>`, exit 1. Missing/empty template ->
   print `NO_TEMPLATE: <path>`, exit 2 (a distinct code: an empty blocklist must not
   masquerade as a clean draft).

### 2. Quote/punctuation normalization (the one real correctness detail)

The table entries are wrapped in quotes and the quote style is not stable across the
repo: `templates/` uses straight quotes (`"..."`), `POC guide.md` uses curly
(`"..."`). A draft could contain either. A naive substring match on the raw table
cell would miss the curly-quote variant.

Normalization, applied identically to both the extracted avoid-phrase and the draft
text before matching:
- Strip the wrapping quote characters from the table cell, so matching is on the
  *inner phrase* (`보험사는 반드시 지급해야 한다`), not its quotation.
- Map curly quotes -> straight, collapse runs of whitespace to a single space, so a
  soft-wrapped draft line still matches.

Match is substring-after-normalization. This is deliberately literal: it is a floor,
not a paraphrase detector.

### 3. Critic consumes it

`critic.md` "What you check" gains one bullet under forbidden expressions: call
`python tools/dao.py check-forbidden-expressions <draft>`, and for each returned hit
emit a finding with `finding_type: "forbidden_expression"` (enum value already
exists), `description` naming the phrase and line. Record the hit count in
`forbidden_literal_hit_count`. The existing semantic P3 pass is unchanged and still
produces its own `forbidden_expression` / `unhedged_inference` findings for the
implied cases -- the tool is a floor under that judgment, not a replacement for it.

Instruction makes explicit: a clean tool result does **not** discharge the semantic
pass; both run every version. Record-only -- `passed` stays the critic's call.

Sync note: edit `.claude/agents/critic.md`, then run `tools/sync_agents.py` to
regenerate `.codex/`+`.agents/` copies. Never hand-edit the generated copies.

### 4. Schema change

`schemas/critic_result.schema.json`: add `forbidden_literal_hit_count`
(integer, minimum 0) to `properties`, mirroring `orphaned_tag_count`. **Not added to
`required`** -- the existing committed critic outputs (CASE_021 v1/v2, CASE_022 v1)
predate this field and must continue to validate. Bump the schema `version` per
repo convention. Go-forward critic runs populate it; historical ones legitimately
omit it.

## Data flow

```
draft_report_v{n}.md ──> critic (per version)
                          │
                          ├─ dao.py check-forbidden-expressions  ──> {clean, hits[]}
                          │        (deterministic floor: listed literal phrases)
                          │
                          ├─ critic's P3 semantic pass            ──> implied/paraphrase findings
                          │        (unchanged; owns everything the floor can't see)
                          │
                          └─ critic_result_v{n}.json
                                 forbidden_literal_hit_count: N
                                 findings: [ {forbidden_expression, ...}, ... ]
                                 passed: <critic judgment>   (record-only)
```

## Error handling

- Missing draft file: `NOT_FOUND`, exit 1 -- caller (critic) treats as a stage
  problem, not a clean pass.
- Missing/empty template: `NO_TEMPLATE`, exit 2 -- distinct from clean. An absent
  blocklist is a setup failure, never silently "0 hits."
- Malformed template table (no parseable avoid column): same `NO_TEMPLATE` path --
  fail loud rather than scan against an empty list.
- The subcommand never writes, never locks, never mutates state; there is no
  partial-failure or rollback surface.

## Testing

New `tests/test_dao_forbidden_expr.py`, mirroring `tests/test_dao_evidence_tags.py`,
against `tmp_path` (never the real trees):

1. **Listed phrase present** -> `clean: false`, hit with correct phrase + line,
   exit 1.
2. **Clean draft** -> `clean: true`, `hits: []`, exit 0.
3. **Curly-quote variant** in the draft while the table is straight-quoted -> still a
   hit (the normalization regression; this is the test that would have caught the
   quote-drift).
4. **Whitespace/soft-wrap variant** -> still a hit.
5. **Near-miss paraphrase** (`당연히 지급되어야 마땅하다`) -> **not** a literal hit,
   `clean: true`. Proves the floor's boundary honestly -- documents that the semantic
   layer, not this tool, owns paraphrases.
6. **Missing draft** -> `NOT_FOUND`, exit 1.
7. **Empty/malformed template** -> `NO_TEMPLATE`, exit 2, not a false clean.
8. **Backward-compat**: an existing CASE_021 `critic_result` (no
   `forbidden_literal_hit_count`) still validates against the bumped schema --
   proves the field is genuinely optional and no committed artifact breaks.

Full suite (`pytest`) must stay green; the schema change is additive/optional so no
existing test or committed artifact should break.

## Future (explicitly deferred, not in this spec)

- **Writer-side self-check**: `draft-report` calling the same subcommand before
  writing v2, to self-correct rather than relying on the critic to catch. Cheaper for
  the writer to avoid than the checker to flag, but a second consumer and scope the
  PoC does not need yet.
- **Consolidation-guard test**: a `pytest` asserting no file *other than*
  `templates/forbidden-expressions.md` restates the table, preventing the doc-level
  drift the 2026-07-17 commit fixed by hand from silently returning. Related but
  separate concern (doc drift, not draft content).

## Files touched

| File | Change |
|---|---|
| `tools/dao.py` | new `cmd_check_forbidden_expressions` + subparser (read-only, `read-*` family) |
| `schemas/critic_result.schema.json` | add optional `forbidden_literal_hit_count`, version bump |
| `.claude/agents/critic.md` | one bullet: call the checker, record hits as findings + count; then `sync_agents.py` |
| `tests/test_dao_forbidden_expr.py` | new, mirrors `test_dao_evidence_tags.py` |
| `CLAUDE.md` | changelog row |

No code change to `document_assembly.py`, no new agent, no new tool file.
