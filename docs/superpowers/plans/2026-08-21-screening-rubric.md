# Screening-report rubric — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> **This project forbids unrequested subagent dispatch** (user global instruction), so execution here is inline via superpowers:executing-plans.

**Goal:** Score a finished screening report 0–100 against the domain logic in `rubric_reference/`, with gates, per-axis quotes, and a schema-validated result — then actually score the four reports in `rubric_test/`.

**Architecture:** Three static artifacts plus one run. A Korean rubric document (`templates/rubric-screening-report.md`) holds the axes, 0–4 anchors, gates, and N/A policy. A JSON Schema (`schemas/screening_rubric_result.schema.json`) makes a score auditable — no quote, no score. An agent definition (`.claude/agents/screening-rubric.md`) tells a scorer how to run it. The scorer reads only the report markdown, so "not stated" is judged by whether the report *declares* the absence.

**Tech Stack:** Python 3 + jsonschema (`tools/_validation.py`, `tools/validate_output.py`), pytest, `tools/dao.py write-contract`, `tools/sync_agents.py`.

**Spec:** `docs/superpowers/specs/2026-08-21-screening-rubric-design.md`

## Global Constraints

- Rubric body (`templates/rubric-screening-report.md`) is **Korean**. Schema descriptions, agent definition, tests, and this plan stay **English** (CLAUDE.md documentation rule; the rubric is the deliverable-facing exception, decided 2026-08-21).
- The scorer never reads `source-cases/`, `data/ground_truth/`, or any upstream contract. Report markdown only (+ `screening_report.json` when a case has one).
- No direct writes to `outputs/`/`data/` — governed writes go through `python tools/dao.py write-contract` (harness-guardrails P2/P5/P7). Calibration artifacts for `rubric_test/` live under `rubric_test/results/`, outside the governed tree, and are validated with `python tools/validate_output.py`.
- Do **not** add `screening_rubric_v1` to `schemas/run_state.schema.json`'s `stage_name` enum in this pass. The unit produces a review artifact, not a pipeline stage; `write-contract` accepts arbitrary contract filenames and `--stage` is optional.
- Axis weights: A1 10, A2 25, A3 15, A4 20, A5 15, A6 15 (sum 100). Verdict: any gate triggered → `blocked`; else `weighted_total >= 80` → `pass`; else `revise`.
- `weighted_total = 100 * Σ_applicable(raw_i / 4 * w_i) / Σ_applicable(w_i)`, rounded to one decimal.
- Never quote or restate ground truth. Never compute a disability rate or assert 증세고정 while scoring — the rubric penalises that in reports; the scorer must not do it either.

---

### Task 1: Result contract + schema tests

**Files:**
- Create: `schemas/screening_rubric_result.schema.json`
- Create: `tests/test_screening_rubric_schema.py`

**Interfaces:**
- Consumes: `schemas/common_component_output.schema.json` (`case_id`, `run_id`, `component`, `status`, `created_at`)
- Produces: contract filename convention `screening_rubric_result*.json` → resolved to this schema by `tools/_validation.py:schema_name_for`. Axis ids `A1`–`A6`; gate ids `G1`–`G5`; finding ids `SR-<n>`.

- [ ] **Step 1: Write the failing test**

```python
"""screening_rubric_result.schema.json -- the auditable-score contract.

A rubric score is only worth something if a reader can check it. Two rules
carry that: every applicable axis cites at least one verbatim quote from the
report it scored, and the result names the exact bytes it scored
(target_report_sha256). Without the first, a score is a vibe; without the
second, a score silently outlives the report it describes -- the same failure
screening_report.schema.json's source_denial_contract_hash exists to prevent.
"""
import copy
import json
from pathlib import Path

import pytest

from _validation import load_registry, validate_instance

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = "screening_rubric_result.schema.json"

VALID = {
    "case_id": "CASE_705",
    "run_id": "RUN_20260821_001",
    "component": "screening-rubric",
    "status": "success",
    "created_at": "2026-08-21T14:00:00+09:00",
    "schema_version": "screening_rubric_result.v0.1",
    "rubric_version": "screening_rubric.v0.1",
    "target_report_path": "rubric_test/screening_report_705.md",
    "target_report_sha256": "a" * 64,
    "axis_scores": [
        {
            "axis_id": "A1",
            "raw": 4,
            "weight": 10,
            "applicable": True,
            "na_reason": None,
            "rationale": "10개 섹션 역할이 모두 채워져 있음",
            "evidence_quotes": ["## 1. 사고와 공통 의료정보"],
        }
    ],
    "weighted_total": 100.0,
    "gates": [
        {"gate_id": "G1", "triggered": False, "quote": None, "section": None,
         "description": "금지표현 리터럴 없음"}
    ],
    "verdict": "pass",
    "findings": [],
}


def _errors(instance):
    schemas, registry = load_registry()
    return validate_instance(instance, SCHEMA, schemas, registry)


def test_valid_result_passes():
    assert _errors(VALID) == []


def test_applicable_axis_without_quote_is_rejected():
    bad = copy.deepcopy(VALID)
    bad["axis_scores"][0]["evidence_quotes"] = []
    assert _errors(bad), "an applicable axis with no quote must not validate"


def test_raw_outside_zero_to_four_is_rejected():
    bad = copy.deepcopy(VALID)
    bad["axis_scores"][0]["raw"] = 5
    assert _errors(bad)


def test_na_axis_may_omit_quotes_but_needs_a_reason():
    ok = copy.deepcopy(VALID)
    ok["axis_scores"][0].update(
        {"applicable": False, "raw": None, "evidence_quotes": [],
         "na_reason": "후유장해 항목이 없는 사건"}
    )
    assert _errors(ok) == []

    bad = copy.deepcopy(ok)
    bad["axis_scores"][0]["na_reason"] = None
    assert _errors(bad), "applicable:false without na_reason must not validate"


def test_triggered_gate_requires_a_quote():
    bad = copy.deepcopy(VALID)
    bad["gates"][0].update({"triggered": True, "quote": None})
    assert _errors(bad), "a triggered gate with no quote must not validate"


def test_bad_sha_is_rejected():
    bad = copy.deepcopy(VALID)
    bad["target_report_sha256"] = "not-a-hash"
    assert _errors(bad)


def test_schema_is_registered_by_filename():
    from _validation import schema_name_for
    assert schema_name_for(Path("screening_rubric_result_v1.json")) == SCHEMA
```

- [ ] **Step 2: Run the test, expect failure**

Run: `python -m pytest tests/test_screening_rubric_schema.py -v`
Expected: every test errors — no `screening_rubric_result.schema.json` in `schemas/`.

- [ ] **Step 3: Write the schema**

`schemas/screening_rubric_result.schema.json`, `allOf`-extending `common_component_output.schema.json`, with:
- `schema_version`: const `screening_rubric_result.v0.1`; `rubric_version`: const `screening_rubric.v0.1`
- `target_report_path`: string; `target_report_sha256`: `^[0-9a-f]{64}$`
- `axis_scores`: array, `axis_id` enum `A1..A6`, `raw` integer 0–4 or null, `weight` number, `applicable` bool, `na_reason` string|null, `rationale` string minLength 1, `evidence_quotes` array of strings
  - conditional: `applicable: true` → `raw` integer 0–4 **and** `evidence_quotes` minItems 1; `applicable: false` → `na_reason` string minLength 1
- `weighted_total`: number 0–100
- `gates`: array, `gate_id` enum `G1..G5`, `triggered` bool, `quote` string|null, `section` string|null, `description` string; conditional: `triggered: true` → `quote` string minLength 1 and `section` string minLength 1
- `verdict`: enum `pass` / `revise` / `blocked`
- `findings`: array of `{finding_id (^SR-[0-9]+$), severity (high|medium|low), section, description, fix_suggestion}`
- `required`: the common fields plus `schema_version`, `rubric_version`, `target_report_path`, `target_report_sha256`, `axis_scores`, `weighted_total`, `gates`, `verdict`, `findings`

- [ ] **Step 4: Run the test, expect pass**

Run: `python -m pytest tests/test_screening_rubric_schema.py -v`
Expected: 7 passed.

- [ ] **Step 5: Regression check**

Run: `python -m pytest tests/test_validation.py tests/test_cross_contract.py -q`
Expected: unchanged pass — a new schema file must not disturb registry loading.

- [ ] **Step 6: Commit**

```bash
git add schemas/screening_rubric_result.schema.json tests/test_screening_rubric_schema.py
git commit -m "feat(rubric): screening-rubric result contract"
```

---

### Task 2: The rubric document (Korean)

**Files:**
- Create: `templates/rubric-screening-report.md`

**Interfaces:**
- Consumes: nothing at runtime; it is the scorer's instruction text.
- Produces: the axis/gate vocabulary the agent and the schema share — `A1`–`A6`, `G1`–`G5`, verdict thresholds.

- [ ] **Step 1: Write the rubric**

Front-matter matching the other `templates/*.md` (`type: Reference`, `title`, `description`, `tags`, `timestamp`). Body sections:

1. **목적과 적용 범위** — what it scores, what it deliberately cannot see (report only), and that it is not ground-truth evaluation.
2. **채점 입력** — the `.md`; the optional JSON twin; the 출처 (`[E#]`) block as the evidence-tier source.
3. **섹션 역할 매핑** — the eight roles, with the three observed shapes (7 / 8 / 10 sections) mapped onto them, so a differently-shaped report is scored by role and an absent role is `applicable:false`.
4. **침묵 대 명시 원칙** — value → full credit; explicit scoped absence ("제공된 자료에서 확인되는 …없음", "라우팅 우선순위상의 어떤 출처도 이 항목을 기재하지 않았습니다") → full credit; silence → penalty. Cite `rubric_reference/의료자문_핵심변수_우선순위.docx`'s 표현 원칙 as origin.
5. **축 A1–A6** — for each: 정의, 근거 문서, 가중치, and five anchors (0/1/2/3/4) written as report behaviour. A2 lists the grade-A variables explicitly (주진단명 / 주요 치료 유형 / 수술 여부 / 최종 임상경과 / 현재 치료상태 / 장해 유형 / 환자 자가응답 기왕력) and the grade-B set it upgrades to when the report shows the record exists. A3 lists the document tiers (A: 진단서·수술기록지·기존 장해진단서/신체감정서; B: 외래·최종 진료기록·주요 검사결과지·입퇴원요약·응급실기록; C: 처방·치료내역·간호기록·비용문서). A4 lists the four case types and their required-document sets from `rubric_reference/유형별 주 필요서류 정리 및 유형분류 프로세스.hwpx`.
6. **게이트 G1–G5** — trigger, why it is categorical, and one Korean example line each.
7. **점수 계산** — the weighted formula, N/A redistribution, verdict thresholds.
8. **채점 결과 기록** — one `screening_rubric_result*.json` per report, per the schema; every applicable axis quotes the report.
9. **채점자가 하지 말아야 할 것** — no disability-rate computation, no 증세고정 judgement, no re-adjudication of the case, no penalty for an absence the report properly scoped.

- [ ] **Step 2: Check it against the corpus by hand**

Run: `grep -n "라우팅 우선순위상의 어떤 출처도" rubric_test/screening_report_705.md | head -3`
Expected: hits — confirms the A2 level-4 anchor quotes language the corpus actually uses, not invented phrasing.

- [ ] **Step 3: Commit**

```bash
git add templates/rubric-screening-report.md
git commit -m "docs(rubric): screening-report scoring rubric (Korean)"
```

---

### Task 3: Scorer agent definition

**Files:**
- Create: `.claude/agents/screening-rubric.md`
- Modify: generated copies via `python tools/sync_agents.py`

**Interfaces:**
- Consumes: `templates/rubric-screening-report.md`, `schemas/screening_rubric_result.schema.json`
- Produces: an agent named `screening-rubric` whose output is one `screening_rubric_result*.json`

- [ ] **Step 1: Write the agent definition**

Front-matter `name: screening-rubric`, `description`, `model: opus`. Body:
- **Guardrails** — follows `harness-guardrails` and `harness-guardrails-dev`; never opens `source-cases/`, `data/ground_truth/`, or upstream contracts; blind like `critic`.
- **Not evaluation** — states that ground-truth comparison remains the deferred isolated Unit 11 service, and that this agent's score is reference-free.
- **Procedure** — read report → run `python tools/dao.py check-forbidden-expressions <path>` for G1's deterministic floor → score A1–A6 with quotes → evaluate G1–G5 → compute total → write result.
- **Output** — `screening_rubric_result_v1.json` through `python tools/dao.py write-contract CASE_ID screening_rubric_result_v1.json --data-file ... --schema-name screening_rubric_result.schema.json --held-by screening-rubric --run-id RUN_ID` for a real case; for a standalone report outside `outputs/`, write beside the report and validate with `python tools/validate_output.py`.
- **Error handling** — schema failure: one self-correction attempt, then halt (P4). A `blocked` verdict is normal output, not a stage failure.

- [ ] **Step 2: Regenerate the mirrors**

Run: `python tools/sync_agents.py`
Expected: `.codex/agents/screening-rubric.toml` and `.agents/skills/...` regenerated; never hand-edited.

- [ ] **Step 3: Verify sync**

Run: `python -m pytest tests/test_sync_agents.py -q`
Expected: pass.

- [ ] **Step 4: Commit**

```bash
git add .claude/agents/screening-rubric.md .codex .agents
git commit -m "feat(rubric): screening-rubric scorer agent"
```

---

### Task 4: Score the corpus and deliver

**Files:**
- Create: `rubric_test/results/screening_rubric_result_705.json` (and `_710`, `_711`, `_712`, `_case021`)
- Create: `docs/run-notes/2026-08-21-screening-rubric-calibration.md`

**Interfaces:**
- Consumes: Tasks 1–3
- Produces: five validated results + a cross-report readout

- [ ] **Step 1: Record the exact bytes scored**

```bash
sha256sum rubric_test/*.md outputs/CASE_021/screening_report.md
```
Each result's `target_report_sha256` is copied from this output, never retyped from memory.

- [ ] **Step 2: Run the deterministic gate floor on each report**

```bash
python tools/dao.py check-forbidden-expressions rubric_test/screening_report_705.md
```
Repeat per report. `NOT_FOUND` / `NO_TEMPLATE` (bare text, not JSON) is a setup failure to surface, never a clean pass.

- [ ] **Step 3: Score each report against the rubric**

Read each report in full, score A1–A6 with at least one verbatim quote per applicable axis, evaluate G1–G5, compute `weighted_total`, set `verdict`. Write each result JSON.

- [ ] **Step 4: Validate every result**

Run: `python tools/validate_output.py rubric_test/results/*.json`
Expected: `PASS` for each, schema `screening_rubric_result.schema.json`. A `SKIP` means the filename did not resolve — rename, do not ignore.

- [ ] **Step 5: Determinism probe on the identical pair**

711 and 712 are byte-identical (md5 `58a5770…`). Compare their axis scores and verdicts. Any divergence is a finding about underspecified anchors and goes in the readout as one — it is not averaged away.

- [ ] **Step 6: Write the calibration readout**

`docs/run-notes/2026-08-21-screening-rubric-calibration.md`: per-report totals and verdicts, which axes discriminated across the four reports and which returned the same score everywhere (an axis that never varies is not measuring), every gate that fired with its quote, the determinism result, and any anchor whose wording proved ambiguous while scoring.

- [ ] **Step 7: Commit**

```bash
git add rubric_test/results docs/run-notes/2026-08-21-screening-rubric-calibration.md
git commit -m "test(rubric): score rubric_test corpus, record calibration"
```

---

## Self-review

- **Spec coverage:** inputs and section roles → Task 2 step 1.3; silence-vs-declaration → Task 2 step 1.4; axes/weights → Global Constraints + Task 2 step 1.5; gates → Task 2 step 1.6 + Task 4 step 2; N/A redistribution → Global Constraints + Task 1 schema conditionals; result contract → Task 1; agent → Task 3; calibration incl. the 711/712 probe and the CASE_021 shape control → Task 4. The spec's out-of-scope items (`후유장해 기간 분류표.zip`, automatic scoring code, ground-truth comparison) have no task, deliberately.
- **Placeholders:** none — every step names a command or the content to write.
- **Type consistency:** `axis_id` `A1`–`A6`, `gate_id` `G1`–`G5`, `finding_id` `SR-<n>`, `screening_rubric_result.v0.1` / `screening_rubric.v0.1` used identically in Tasks 1, 3, and 4.
- **Known gap carried, not fixed:** `templates/registry.json`'s `screening_report` entry does not match any rendered report on disk. Recorded in the spec; out of scope here.
