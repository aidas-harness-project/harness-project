# Medical Variable Extraction and Hierarchical Organization Implementation Plan

> **For Hermes:** Use subagent-driven-development and strict test-driven development to implement this plan task-by-task only after the user approves the decisions below.

**Goal:** Add a canonical, evidence-linked, longitudinal medical-variable contract and organize it as case → medical domain → variable → observation, with contextual A/B/C importance assignments, without creating a new top-level pipeline stage or duplicating medical truth across outputs.

**Architecture:** Implement medical structuring as the first internal checkpoint of the existing `claim_analysis` stage. The current `medical_variables.json` plus its DAO-owned, content-addressed immutable revision history form one canonical medical-fact family; the existing `extracted_claim_fields.json` remains a compatibility projection generated from the current canonical medical contract plus non-medical claim fields. JSON Schema provides shape validation, while a purpose-built DAO write path performs cross-reference, hierarchy, source-coverage, and evidence-resolvability checks before any output is published.

**Tech Stack:** Python 3, JSON Schema Draft 2020-12, `jsonschema`, the existing `tools/dao.py` boundary, pytest, React/Vite frontend, existing npm workflow.

---

## 1. Scope and requirement map

This plan covers:

- `MED-DAT-001` through `MED-DAT-012`
- `MED-DOC-001` through `MED-DOC-006`
- `MED-QTY-001` through `MED-QTY-007`
- `MED-IMP-001` through `MED-IMP-007`
- `MED-SRC-001` through `MED-SRC-007` for medical variables
- `MED-RTE-001` through `MED-RTE-004` and `MED-RTE-007`
- M1, M2, and M3 acceptance criteria in `docs/medical-appropriateness-screening-requirements.md`

This plan does not implement:

- anomaly-severity scoring or medical-issue referral logic;
- medical expert request lifecycle or response capture, which is covered by the companion review-harness plan;
- medical-answer retrieval/reuse;
- ground-truth-backed evaluation metrics;
- automatic disability, causation, treatment-appropriateness, coverage, denial, or reduction judgments.

## 2. Proposed decisions requiring user review

Implementation must not begin until the user approves or changes V1-V7. After that approval, schemas, validators, disabled configuration, and synthetic tests may be built before clinical-policy approval; extraction behavior remains disabled for unapproved case/document types and unapproved codebooks.

### Decision V1: integration boundary

**Proposal:** Add medical structuring as checkpoint 1 inside the existing `claim_analysis` stage, then renumber its current four checkpoints to 2-5. Do not add a top-level stage or a new autonomous agent.

**Reasoning:** This preserves the source proposal's “transformation layer, not more process steps” intent, gives medical structuring a distinct validated contract, and avoids agent/stage abstraction bloat. Run-state remains `claim_analysis`; checkpoint resumability is represented by the presence and validation status of the individual contracts, as the current claim-analysis documentation already intends.

Resolves: `MED-OQ-001` for the initial PoC.

### Decision V2: canonical contract and compatibility

**Proposal:**

- Canonical medical truth: `outputs/CASE_XXX/medical_variables.json`.
- Each successful publication also preserves an immutable content-addressed revision at the DAO-owned protected path `outputs/CASE_XXX/_medical_variable_revisions/<sha256>.json`, keyed by the full SHA-256 of the validated canonical bytes. The current file and revision archive have the same schema and writer; the archive is history, not a second maintained representation, and no agent receives the archive path.
- Existing `extracted_claim_fields.json`: compatibility projection only for current downstream consumers.
- No agent may independently re-extract a medical field into the legacy file.
- Every projected medical field carries `source_variable_ids`/`source_observation_ids`, and a deterministic adapter creates it from `medical_variables.json`.

**Reasoning:** This preserves compatibility without creating two independently maintained definitions of diagnoses, dates, treatment, or medical history.

Resolves part of `MED-OQ-002` and defines the migration direction for `MED-OQ-020`.

### Decision V3: hierarchy semantics

**Proposal:** Use one containment hierarchy:

```text
case
  └── medical domain
        └── variable
              └── observation(s over time)
```

Initial domain codes:

1. `diagnosis`
2. `objective_evidence`
3. `treatment`
4. `clinical_course`
5. `history_and_prognosis`
6. `function_and_disability_evidence`
7. `missing_evidence`
8. `context`

A/B/C importance is not a parent/child hierarchy and is not permanent metadata on a variable type. It is an issue-contextual assignment attached to a variable or observation with `issue_context`, `tier`, `reason`, and `codebook_version`.

The contract also contains `medical_issues[]`: small, evidence-linked groupings that name one medical question or unresolved topic and reference the observations that converge on it. Each link classifies the observation's relation to that issue as `supports`, `weakens`, `conflicts`, `contextual`, or `missing_expected`, and its issue-specific materiality as `core`, `supporting`, or `context`. The relation and materiality require a reason and version. An issue remains organizational context, not an anomaly score, referral decision, or medical conclusion. This gives importance assignments and the review harness a stable, auditable evidence set without requiring the unimplemented referral policy.

**Reasoning:** The same observation may be Tier A for one question and Tier B for another. Treating the letter as a fixed folder would violate `MED-IMP-001`, `MED-IMP-006`, and `MED-IMP-007`.

Resolves the data-organization part of `MED-OQ-017`; clinical approval is still required under `MED-OQ-014`.

### Decision V4: typed values and epistemic state

**Proposal:** Each observation has an explicit `value_type` and exactly one matching typed payload:

- `coded_value`
- `text_value`
- `temporal_value`
- `numeric_value`
- `quantity_value`

Each observation also has one `value_state`:

- `asserted`
- `unknown`
- `absent`
- `not_applicable`

Contradiction is not a value state. Every competing source assertion remains a separate `asserted` observation with its own typed value and evidence. A top-level `contradiction_groups[]` entry links at least two competing observation IDs to the required same-case P6 conflict-ledger ID. The medical artifact never copies P6 status: validation/read-time resolution obtains it from `_conflict_ledger.json`, so P6 remains the single source of truth. The medical contract never collapses competing assertions into one multi-valued observation or resolves the conflict itself.

Dates never acquire fabricated precision. An asserted `temporal_value` is a closed point/range union: a point is `{kind: "point", precision, value, raw_text}` and a range is `{kind: "range", start: {precision, value}, end: {precision, value}, raw_text}`. Endpoint precision is `day`, `month`, or `year`; values are respectively `YYYY-MM-DD`, `YYYY-MM`, or `YYYY`. `raw_text` preserves the source expression when precision is less than day or normalization is uncertain. The separate `observed_at` field additionally permits `{kind: "unknown", raw_text}` because an asserted diagnosis/treatment may have an unknown occurrence date; an unknown medical value itself uses `value_state: unknown` and carries no typed payload.

Numeric/quantity units use `{unit_status, unit_code, raw_unit}`. `normalized` requires an approved `unit_code`; `unmapped` requires `raw_unit` and forbids aggregates that assume unit equivalence. This makes storage executable before a clinical unit codebook is approved without silently normalizing unknown units.

Resolves the generic representation part of `MED-OQ-017`; approved unit vocabularies and clinical counting equivalences remain deferred.

### Decision V5: source locator

**Proposal:** Introduce `medical_common.schema.json#/$defs/evidence_locator` rather than expanding the repository-wide common evidence reference in the first slice. Required fields are:

- `document_id`
- `page`
- exactly one of `quote` or `table_location`

Optional fields are:

- `document_name`
- `document_date`
- `chunk_id`
- `section`
- `page_region`

`table_location` is the only table representation and contains `table`, `row`, `column`, and `value`; those fields never also appear at locator top level.

The DAO resolves `document_id`, page, and optional `chunk_id` against `document_manifest.json` and `page_chunks.json`. `document_name` and `document_date` are copied display metadata, not identifiers.

**Reasoning:** Medical requirements need richer provenance, but changing the global evidence shape would unnecessarily widen the initial migration.

Resolves the PoC choice for `MED-OQ-007`; the exact `page_region` representation remains deferred.

### Decision V6: document-focus and hierarchy configuration

**Proposal:** Store the versioned medical domain hierarchy, document-focus depths, variable kinds, importance-tier definitions, and allowed unit/code vocabularies in one canonical file:

`config/medical/medical_structuring_v0.1.json`

Validate it with `schemas/medical_structuring_config.schema.json`. Agent prompts link to this configuration and must not copy its rule tables.

**Reasoning:** One configuration is the single change point for the initial PoC while avoiding a directory of speculative micro-codebooks.

Resolves the storage shape for `MED-OQ-009` and part of `MED-OQ-002`; medical approval of counting rules remains required.

### Decision V7: initial clinical slice

**Proposal:** After user approval of this plan, implement the schema and validation generically, but leave extraction disabled for every case type and document role not selected by the user/medical lead. Unsupported document types produce explicit `coverage_status: unsupported_needs_configuration`; they are not silently skipped. Clinical behavior is enabled only by a versioned configuration approval, never by the mere presence of generic schema code.

Still requires an answer to `MED-OQ-015`.

## 3. Canonical data contract

The implementation should use these stable identities:

- Domain: `MD_<code>` from configuration, not model-generated free text.
- Medical issue: `MCI_0001`, unique within a case contract.
- Issue-evidence link: `MIL_0001`, unique within a case contract.
- Variable: `MV_0001`, unique within a case contract.
- Observation: `MO_0001`, unique within a case contract.
- Evidence locator: `MEV_0001`, unique within a case contract even when multiple observations cite the same source location.
- Contradiction group: `MCG_0001`, unique within a case contract.
- Importance assignment: `MI_0001`, unique within a case contract.
- Quantity summary: `MQ_0001`, unique within a case contract.
- Medical-variable revision: full lowercase SHA-256 plus `run_id`, `schema_version`, and `config_version`; the digest is minted from the canonical serialized bytes by the DAO, never supplied by an agent.

Minimum top-level shape:

```json
{
  "case_id": "CASE_009",
  "run_id": "RUN_20260723_001",
  "component": "claim-analysis",
  "status": "success",
  "schema_version": "medical_variables.v0.1",
  "config_version": "medical_structuring.v0.1",
  "source_coverage": [],
  "domains": [],
  "medical_issues": [],
  "variables": [],
  "contradiction_groups": [],
  "importance_assignments": [],
  "quantity_summaries": [],
  "timeline_observation_ids": []
}
```

### 3.1 Field-level shapes

These shapes are the implementation baseline; Task 3 translates them into complete JSON Schema rather than designing them ad hoc.

| Object | Required fields | Rules |
|---|---|---|
| Source-coverage entry | `document_id`, `document_role`, `coverage_status`, `reason`, `processing_contract_versions` | One entry for every DAO-exposed, non-ground-truth input document. Status is `consumed`, `human_only_not_consumed`, `unsupported_needs_configuration`, `blocked_upstream`, or `out_of_scope`. |
| Domain | `domain_id`, `domain_code`, `label`, `config_version` | Code must exist in the approved configuration. Domains have no model-created parent links. |
| Variable | `variable_id`, `domain_id`, `variable_kind`, `label`, `observations` | `variable_kind` must be configured. The v0.1 hierarchy is exactly one variable level; related variables use non-owning relation IDs rather than parent-variable containment. |
| Observation | `observation_id`, `value_state`, `observed_at`, `evidence`, `extraction_method` | `observed_at` uses the precision-preserving temporal shape or explicit `unknown`. Source-grounded asserted observations require evidence. |
| Medical issue | `issue_id`, `issue_category`, `question`, `evidence_links`, `config_version` | One focused question/topic. It contains no severity, referral decision, or medical conclusion. |
| Issue-evidence link | `issue_evidence_link_id`, `observation_id`, `relation`, `materiality`, `reason`, `classification_version` | `relation` is `supports`, `weakens`, `conflicts`, `contextual`, or `missing_expected`; materiality is `core`, `supporting`, or `context`. |
| Importance assignment | `importance_id`, `issue_id`, exactly one of `variable_id`/`observation_id`, `tier`, `reason`, `config_version` | Tier is A/B/C and cannot itself trigger referral. |
| Contradiction group | `contradiction_group_id`, `observation_ids`, `conflict_id` | At least two asserted observations. `conflict_id` must resolve in this case's P6 ledger. Status and resolution remain exclusively in P6 and are never copied. |
| Quantity summary | `quantity_id`, `variable_id`, `source_observation_ids`, `period`, `count`, `unit_status`, `unit_code`/`raw_unit`, `counting_basis`, `coverage_status` | It is a derived index. All source observations remain canonical; incomplete coverage cannot be labeled complete, and unmapped units cannot be combined as equivalent. |

### 3.2 Observation value union

For `value_state: asserted`, exactly one payload is present:

| `value_type` | Payload |
|---|---|
| `coded` | `coded_value: {system, code, display}` |
| `text` | `text_value: string` |
| `temporal` | `temporal_value` point/range union with precision-preserving endpoint values and `raw_text` |
| `numeric` | `numeric_value: {number, unit_status, unit_code?, raw_unit?}` with the status-dependent unit rule from V4 |
| `quantity` | `quantity_value: {count, unit_status, unit_code?, raw_unit?, period_start, period_end, counting_basis}` with the same unit rule |

For `unknown`, `absent`, or `not_applicable`, no typed payload is allowed and `state_reason` is required. A source-grounded claim that a finding is absent uses `absent` plus evidence; lack of documentation uses `unknown`, never `absent`.

### 3.3 Evidence locator

Each locator has a stable `locator_id`, `document_id`, `page`, and exactly one of `quote` or `table_location`. Optional copied display metadata are `document_name`, `document_date`, `chunk_id`, `section`, and `page_region`. `table_location` contains `table`, `row`, `column`, and `value`. The DAO resolves identifiers and page/chunk membership; copied names/dates are never used as identity.

### 3.4 Existing contracts this plan relies on

- `document_manifest.json` is the canonical processed-document inventory and source disposition input.
- `page_chunks.json` is the canonical processed-text/chunk source for automated extraction.
- P8 must already be agreed or human-resolved before a page is eligible for medical extraction.
- P6 `_conflict_ledger.json` remains the authority for factual-contradiction status and resolution; this contract stores only its stable `conflict_id`.
- D1 prevents ground-truth files from entering the manifest/chunk inputs available to claim analysis.
- `expert_review_only`/human-only source dispositions are listed in source coverage but their content is not consumed by the automated extractor.

Source-coverage status is deterministic: `consumed` requires an enabled role, completed/validated chunks, and resolved P8 status; `human_only_not_consumed` mirrors a human-only text disposition; `unsupported_needs_configuration` means the role/type has no enabled extractor rule; `blocked_upstream` means required OCR/schema/P8 input is not ready; `out_of_scope` means an approved configuration intentionally excludes the non-medical role. The model may not choose these statuses, and a document cannot be absent from the coverage array merely because it was not consumed.

Important invariants beyond JSON Schema:

1. Every variable references one configured domain.
2. Every observation belongs to exactly one variable.
3. Variables have no containment parent in v0.1; every relation ID is non-owning and resolves without changing the fixed domain → variable → observation hierarchy.
4. Every source-grounded asserted observation has at least one resolvable evidence locator.
5. Unknown/absent/not-applicable observations cannot carry fabricated typed values; only a source-grounded absence may carry evidence.
6. Every contradiction group retains each competing asserted observation and resolves against a valid same-case P6 conflict record; no P6 status is duplicated.
7. Every importance assignment references an existing variable or observation and includes case/issue context, reason, and config version.
8. Every medical issue references existing observations, classifies issue-specific relation/materiality with reasons, states one focused uncertainty/question, and contains no anomaly threshold, referral decision, or medical conclusion.
9. A/B/C alone never sets `review_required` or triggers expert referral.
10. Timeline and quantity summaries reference canonical observation IDs; they do not copy facts into a second maintained representation.
11. Source coverage lists every eligible input document exactly once as `consumed`, `human_only_not_consumed`, `unsupported_needs_configuration`, `blocked_upstream`, or `out_of_scope`.

## 4. Implementation tasks

### Task 1: Lock the approved v0.1 decisions

**Objective:** Convert the user's review outcome into an executable scope without inventing unresolved medical rules.

**Files:**

- Modify: `docs/medical-appropriateness-screening-requirements.md` only if the user approves resolutions to `MED-OQ-*` entries.
- Modify: `open-decisions.md` only if the repository's existing decision register needs a cross-link.

**Steps:**

1. Record approved choices V1-V7 and explicitly list deferred items.
2. Record the medical lead responsible for variable-kind, domain, importance, and counting-rule approval, or record `unassigned` and keep clinical behavior disabled.
3. Record the first PoC case types/document roles, or leave the enabled set empty so every type is explicitly unsupported pending configuration.
4. Define the objective medical approval artifact for later behavior activation; user approval of this implementation plan is not clinical-policy approval, and silence is never approval.
5. Run `git diff --check`.

### Task 2: Add the canonical medical configuration under TDD

**Objective:** Establish one versioned source for hierarchy and document-focus rules.

**Files:**

- Create: `schemas/medical_structuring_config.schema.json`
- Create: `config/medical/medical_structuring_v0.1.json` with `behavior_enabled: false` and no enabled case/document roles until the recorded medical approval exists.
- Modify: `tests/test_validation.py`

**RED tests:**

- the disabled bootstrap configuration validates, and enabling behavior without approval metadata fails;
- duplicate domain/variable codes fail semantic validation;
- unsupported document roles are explicit;
- importance tiers are exactly A/B/C and carry descriptions;
- quantity rules require a counting basis and allowed units;
- no configuration entry encodes an automatic denial/reduction/appropriateness verdict.

**Commands:**

```bash
pytest tests/test_validation.py -v
python tools/validate_output.py config/medical/medical_structuring_v0.1.json --schema-name medical_structuring_config.schema.json
```

If `validate_output.py` cannot validate non-output configuration by explicit schema today, add that capability test-first rather than bypassing validation.

### Task 3: Add shared medical definitions and the variable schema

**Objective:** Define the canonical variable/observation contract and reusable medical identifiers once.

**Files:**

- Create: `schemas/medical_common.schema.json`
- Create: `schemas/medical_variables.schema.json`
- Modify: `tests/test_validation.py`

`medical_common.schema.json` owns only definitions shared across medical contracts: medical issue/issue-evidence-link/variable/observation/importance IDs, issue categories, evidence locators, source-coverage status, schema/config version formats, the closed medical-variable revision reference (`sha256`, `run_id`, `schema_version`, `config_version`), and precision-preserving temporal point/range shapes. Reviewer roles, specialties, actors, request states, and response provenance belong to the review plan's separate `medical_review_common.schema.json`.

**RED-GREEN slices:**

1. Minimal diagnosis variable with one asserted coded observation.
2. Multiple diagnosis observations over time.
3. Imaging/laboratory/functional/pathology observations without flattening.
4. Surgery, procedure, injection, manual therapy, and rehabilitation observations.
5. Clinical-course observations and response-to-treatment.
6. Prior condition, comorbidity, prognostic factor, and objective function.
7. Missing-evidence record with blocked question.
8. Unknown, absent, and not-applicable states plus separate contradiction groups over asserted observations.
9. A/B/C contextual assignments with reasons and versions.
10. Evidence-linked medical issue groupings that carry no referral verdict.
11. Quantity summaries with date range, coverage, counting basis, and source observations.

For each slice, write the failing test, run the exact test and observe the expected failure, add the minimal schema, then rerun to green.

### Task 4: Add semantic and referential validation

**Objective:** Enforce invariants JSON Schema cannot express.

**Files:**

- Create: `tools/medical_contracts.py`
- Create: `tests/test_medical_contracts.py`
- Modify: `tools/_validation.py` only if schema-specific semantic errors should be surfaced by the common validation command.

**Functions to implement:**

```python
def validate_medical_variables(data: dict, *, manifest: dict, page_chunks: dict, conflict_ledger: dict, config: dict) -> list[str]: ...
def build_timeline_index(data: dict) -> list[str]: ...
def project_legacy_medical_fields(data: dict, *, projection_config: dict) -> dict: ...
```

**Required tests:**

- missing IDs and dangling references fail;
- duplicate IDs fail;
- any containment parent field fails in v0.1; non-owning relation IDs must resolve;
- evidence pointing to an unknown document/page/chunk fails;
- evidence from `expert_review_only` text-excluded documents cannot be consumed by the automated extractor;
- competing observations grouped as contradictory without a P6 conflict ID fail;
- unknown or cross-case conflict IDs and malformed P6 records fail; copied P6 `status`/resolution fields in a contradiction group fail closed;
- importance assignment without context/reason/version fails;
- medical issue with dangling observations or verdict-like fields fails;
- every DAO-exposed non-ground-truth manifest document has exactly one source-coverage entry;
- human-only, unsupported, and P8-blocked documents cannot be marked consumed;
- timeline order is deterministic and does not duplicate observation content;
- quantity summaries cannot claim complete coverage when source coverage is partial.

### Task 5: Add a purpose-built DAO write boundary

**Objective:** Ensure agents cannot publish medical variables with unresolved references or bypass protected data paths.

**Files:**

- Modify: `tools/dao.py`
- Modify: `tests/conftest.py`
- Create: `tests/test_dao_medical_variables.py`

**CLI:**

```text
python tools/dao.py read-medical-variables CASE_ID
python tools/dao.py read-medical-variables CASE_ID --revision-sha SHA256
python tools/dao.py read-medical-evidence CASE_ID LOCATOR_ID [--revision-sha SHA256]
python tools/dao.py write-medical-variables CASE_ID \
  --data-file /tmp/medical_variables.json \
  --held-by claim-analysis \
  --run-id RUN_YYYYMMDD_N
```

**Behavior:**

1. Read validated `document_manifest.json`, `page_chunks.json`, and the same-case P6 `_conflict_ledger.json` through DAO-owned paths.
2. Refuse ground-truth entries before constructing the extractor-visible document set.
3. Classify every remaining document as consumed, human-only, unsupported, blocked upstream, or out of scope using manifest/P8/config state.
4. Load the medical configuration and reject real extraction unless `behavior_enabled` and the case/document role are approved; disabled configuration remains usable only for schema and synthetic validator tests.
5. Run JSON Schema plus semantic/resolvability/source-coverage validation, including conflict-ID existence and same-case ownership against the P6 ledger.
6. Acquire `medical_variables.json.lock` before publication.
7. Re-read the manifest, page chunks, P6 ledger, and configuration after the publication lock before final validation. P6 status changes do not invalidate the candidate because status is not copied, but the referenced immutable conflict record must still exist and belong to the case.
8. Canonicalize the validated object as UTF-8 JSON using sorted keys, `ensure_ascii=False`, separators `(',', ':')`, and no trailing newline; compute the full lowercase SHA-256 of those exact bytes; and atomically preserve an immutable revision before updating the current `medical_variables.json` content. If the digest already exists, verify byte equality and reuse it; a digest collision/mismatch fails closed. Neither agents nor review callers can write revisions directly or receive a filesystem path. This slice exposes no revision-delete/prune command; any future retention feature must first prove that no medical-review ledger references the revision.
9. Return stable machine-readable PASS/FAIL output.
10. Do not mark the whole `claim_analysis` stage passed; only later checkpoint orchestration may do so.
11. `read-medical-variables` returns the current validated contract or an exact immutable revision through the DAO boundary. `read-medical-evidence` accepts an optional DAO-validated revision SHA, proves locator membership in that exact revision, and enforces current case/source classification/D1. For a historical revision it returns the exact immutable `quote` or `table_location` stored in that revision and does not substitute current page/chunk text; surrounding mutable context is outside v0.1. For current reads it may use the existing validated page-text path. It never accepts a caller-supplied filesystem path.

**RED tests:** invalid refs do not persist, unknown/cross-case P6 references do not persist, lock contention does not corrupt output, stale reads are detected/revalidated, identical bytes reuse one revision, changed bytes preserve the old revision while advancing current, digest/byte mismatch fails closed, revision reads are case-scoped, and no real `outputs/` or `data/` paths are touched.

### Task 6: Make the legacy claim card a deterministic projection

**Objective:** Preserve current downstream compatibility while keeping `medical_variables.json` as the only maintained medical truth.

**Files:**

- Modify: `schemas/extracted_claim_fields.schema.json`
- Modify: `tools/medical_contracts.py`
- Create: `schemas/medical_projection_config.schema.json`
- Create: `config/medical/medical_projection_v0.1.json`
- Create: `tests/test_medical_claim_projection.py`
- Modify: `.claude/agents/consistency-check.md`
- Modify: `.claude/agents/screening-report.md`
- Modify: `.claude/agents/denial-validation.md`

**Changes:**

- Add `source_variable_ids` and `source_observation_ids` to projected medical fields.
- Generate diagnosis, KCD, diagnosis/onset dates, surgery, hospital/treatment periods, and other retained medical compatibility fields from the canonical contract.
- Preserve non-medical claim fields in their existing path.
- Move the existing primary-diagnosis selection behavior from `.claude/agents/claim-analysis.md` verbatim into the versioned projection configuration and pin it with golden tests before changing the prompt. This is a migration of current behavior, not a new clinical rule.
- For every other headline field: project one distinct, unconflicted canonical candidate; merge exact normalized duplicates while preserving all source observation IDs; apply an explicitly configured approved rule when one exists; otherwise return `requires_human_projection` and do not publish a successful legacy contract.
- Record `projection_rule_id` and `projection_config_version` for every selected headline.
- Preserve all contradictory source observations in `medical_variables.json` and the P6 ledger even when one compatibility headline is selected.

**Verification:** A test mutating the canonical observation changes the projection; ambiguous multi-candidate input fails closed; the existing primary-diagnosis fixtures produce the same result before and after migration; there is no separate extraction path that can drift.

### Task 7: Update the claim-analysis checkpoint contract

**Objective:** Make the agent produce and consume the new contract in the intended order.

**Files:**

- Modify: `.claude/agents/claim-analysis.md`
- Modify: `.claude/skills/loss-adjustment-pipeline/SKILL.md`
- Modify: `pipeline.md`
- Regenerate: `.codex/agents/claim-analysis.toml`
- Regenerate: `.codex/agents/consistency-check.toml`
- Regenerate: `.codex/agents/screening-report.toml`
- Regenerate: `.codex/agents/denial-validation.toml`
- Regenerate: `.agents/skills/loss-adjustment-pipeline/SKILL.md`
- Run: `python tools/sync_agents.py`

**Checkpoint order:**

1. Medical variable extraction and hierarchy → `medical_variables.json`.
2. Claim fact compatibility projection/non-medical field extraction → `extracted_claim_fields.json`.
3. Coverage identification.
4. Case-type classification.
5. Requirement matching.

**Guardrails:**

- processed/validated text only;
- no second OCR pass;
- no answer-key access;
- no unsupported final medical inference;
- P6 for factual contradictions;
- A/B/C does not create a referral;
- unsupported document role becomes explicit configuration review.

### Task 8: Add hierarchical API and UI presentation

**Objective:** Let an authorized human inspect the hierarchy and navigate to evidence without using a generic JSON dump.

**Files:**

- Modify: `frontend/backend/main.py`
- Modify: `tests/test_frontend_review_endpoints.py`
- Modify: `frontend/web/src/api.js`
- Create: `frontend/web/src/components/MedicalVariablesPanel.jsx`
- Modify: `frontend/web/src/components/StageDetail.jsx`
- Modify: `frontend/web/src/pipelineDefinition.js`
- Modify: `frontend/web/src/staticCaseData.js` only with synthetic demonstration data.

**UI behavior:**

- the backend obtains medical contracts and source snippets only through the purpose-built DAO read commands, never by opening `outputs/`, `data/`, or source ledgers directly;
- domain sections expand to variables and chronological observations;
- value state and source coverage are visible;
- A/B/C is shown with context and reason, not as a static folder label;
- quantity summaries show counting basis/date range/coverage;
- contradictory values remain side by side;
- source actions use an evidence-scoped backend endpoint and never expose ground-truth files;
- the UI labels the current localhost/no-auth limitation.

**Tests:**

- API rejects unknown/dangling observation IDs;
- source navigation cannot cross case boundaries or serve ground truth;
- page/chunk returned matches the locator;
- frontend lint and build pass.

### Task 9: Add synthetic end-to-end contract fixtures

**Objective:** Prove the complete extraction hierarchy on safe synthetic data.

**Files:**

- Create: `tests/fixtures/medical/diagnosis_imaging_treatment.json`
- Create: `tests/fixtures/medical/contradiction_and_missing_evidence.json`
- Create: `tests/fixtures/medical/repeated_treatment_partial_coverage.json`
- Create: `tests/test_medical_structuring_integration.py`

Fixtures must contain invented people/documents only and must not be copied from `source-cases/`, `outputs/`, `data/`, or the answer key.

**Scenarios:**

1. Diagnosis + code + diagnosis date + imaging + surgery.
2. Multiple observations over time with clinical course.
3. Prior condition/prognostic factor separated from current diagnosis.
4. Contradictory source values linked to P6.
5. Missing critical evidence.
6. Repeated treatment with explicitly partial source coverage.
7. Context-only Tier C assignment that triggers no referral behavior.

### Task 10: Documentation, migration, and full verification

**Objective:** Make the contract adoptable without hidden compatibility assumptions.

**Files:**

- Modify: `README.md`
- Modify: `pipeline.md`
- Modify: `known-gaps.md`
- Modify: `docs/medical-appropriateness-screening-requirements.md` only for approved decision resolutions and implemented-status links.

**Document:**

- canonical ownership and compatibility projection;
- rerun/invalidation when source text, config, or medical variables change;
- old-run behavior when `medical_variables.json` is absent;
- explicit unsupported/deferred clinical rules;
- recovery from failed checkpoint writes;
- source access and PII boundary;
- version bump rules.

**Final commands:**

```bash
pytest tests/test_validation.py tests/test_medical_contracts.py tests/test_dao_medical_variables.py tests/test_medical_claim_projection.py tests/test_medical_structuring_integration.py tests/test_frontend_review_endpoints.py -v
pytest -q
python tools/sync_agents.py
npm run lint --prefix frontend/web
npm run build --prefix frontend/web
git diff --check
```

Inspect `git diff` to prove generated copies match canonical `.claude` definitions and no raw/protected paths were modified.

## 5. Verification matrix

| Requirement | Proof |
|---|---|
| Longitudinal medical representation | Schema and fixtures with repeated observations |
| Hierarchical organization | Domain/variable/observation referential tests and UI |
| A/B/C contextual importance | Assignment schema + non-trigger tests |
| No loss of contradictions | P6 link and side-by-side UI tests |
| Source grounding | Purpose-built DAO resolvability tests |
| Treatment quantities are contextual | Schema prohibition/config review + no referral rule |
| One medical SSOT | Current-plus-immutable-revision DAO tests, projection tests, and search for independent legacy extraction |
| No new top-level stage | Unchanged run-state stage enum and pipeline tests |
| No ground-truth leakage | API/DAO boundary tests |
| Safe compatibility | Legacy projection and older-run migration tests |

## 6. Risks and mitigations

- **Clinical vocabulary finalized by developers:** block behavior/config approval on a named medical lead; schemas can be implemented generically first.
- **Two sources of medical truth:** only the DAO may publish the current canonical artifact and its byte-identical immutable revisions; only deterministic projection may populate legacy medical fields; search and tests must reject independent extraction instructions.
- **Hierarchy overfitting:** keep fixed top-level domains small and use versioned variable kinds rather than arbitrary nested free text.
- **Static A/B/C labels:** model importance as issue-contextual assignments, not variable-type metadata.
- **Schema-valid but referentially invalid output:** enforce semantic validation in the purpose-built DAO command.
- **Partial records presented as complete:** require source coverage and counting scope on aggregates.
- **UI exposes PII/ground truth:** use evidence-scoped backend access, preserve D1 checks, and keep frontend localhost-only until authentication is separately approved.
- **Breaking older runs:** treat missing `medical_variables.json` as legacy mode with an explicit badge/warning; do not silently synthesize the canonical contract from old output.

## 7. Implementation order relative to the companion plan

Complete Tasks 1-5 of this plan before the review-request harness consumes medical variables. The review plan may then implement its schemas and DAO lifecycle against `medical_common.schema.json` and validated observation IDs.

The plans intentionally share integration files and must not edit them from stale snapshots:

1. Complete this plan's Task 7 before the review plan's Task 8 edits `.claude/agents/claim-analysis.md`, `.claude/skills/loss-adjustment-pipeline/SKILL.md`, and generated copies. The second task re-reads and preserves the first task's checkpoint changes, then runs `sync_agents.py` and both instruction-level test sets.
2. Complete this plan's Task 8 before the review plan's Tasks 9-10 edit `frontend/backend/main.py`, `tests/test_frontend_review_endpoints.py`, `frontend/web/src/api.js`, `StageDetail.jsx`, `pipelineDefinition.js`, or synthetic static data. The review UI task integrates both panels and reruns hierarchy plus review endpoint/frontend tests.
3. Apply documentation tasks sequentially, re-reading `README.md`, `pipeline.md`, `known-gaps.md`, and the requirements document before the second edit. The final diff must describe both contracts without duplicating ownership.

## 8. Commit policy

Do not commit automatically. If the user authorizes commits during implementation, use explicit path lists only and split commits by semantic unit:

1. medical schemas/config/tests;
2. semantic validator + DAO boundary;
3. compatibility projection + agent/pipeline sync;
4. frontend hierarchy/source navigation;
5. documentation/migration.
