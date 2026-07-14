// Static structure of the pipeline -- mirrors pipeline.md. The dynamic part
// (status, timestamps, attempt counts) comes from the backend's run-state;
// this file only describes the shape that never changes between cases.

export const PHASE_1 = [
  {
    key: "case-intake",
    label: "Case Intake",
    agent: null,
    aliases: ["case_intake", "intake"],
    description: "Splits the source case into model input and ground truth, gated by a human-approved, per-file ledger.",
    checkpoints: [],
    gate: "source-ledger",
  },
  {
    key: "document-pipeline",
    label: "Document Processing",
    agent: "document-pipeline",
    aliases: ["document_processing"],
    description: "OCR with dual-path cross-validation, classification, redaction, chunking.",
    checkpoints: [
      { key: "ocr-crossval-classify", label: "OCR + cross-validation + classification" },
      { key: "redaction", label: "Redaction" },
      { key: "chunking", label: "Chunking" },
    ],
    gate: "ocr-review",
  },
  {
    key: "indexing",
    label: "Indexing (adapter)",
    agent: null,
    description: "Pass-through by default -- no-op unless real indexing is enabled.",
    checkpoints: [],
  },
  {
    key: "policy-pipeline",
    label: "Policy Clause Processing",
    agent: "policy-pipeline",
    description: "Clause boundary identification, extraction, normalization into standard fields.",
    checkpoints: [
      { key: "boundary", label: "Boundary identification" },
      { key: "extraction", label: "Clause extraction" },
      { key: "normalization", label: "Normalization" },
    ],
  },
  {
    key: "claim-analysis",
    label: "Claim Analysis",
    agent: "claim-analysis",
    description: "Core field extraction, coverage identification, case-type classification, requirement matching.",
    checkpoints: [
      { key: "field-extraction", label: "Field extraction" },
      { key: "coverage", label: "Coverage identification" },
      { key: "case-type", label: "Case-type classification" },
      { key: "requirement-matching", label: "Requirement matching" },
    ],
    contracts: ["extracted_claim_fields.json", "coverage_result.json", "case_type_result.json", "requirement_matching_result.json"],
  },
  {
    key: "consistency-check",
    label: "Consistency Check",
    agent: "consistency-check",
    description: "Cross-references claims against source documents for internal contradictions.",
    checkpoints: [],
    gate: "conflict-ledger",
    contracts: ["evidence_validation_result.json"],
  },
  {
    key: "screening-report",
    label: "Screening Report",
    agent: "screening-report",
    description: "Internal triage document assembled from every structured Phase 1 output.",
    checkpoints: [],
    report: "screening_report.md",
  },
  {
    key: "draft-report-v1",
    label: "Draft Report (v1)",
    agent: "draft-report",
    description: "The actual deliverable -- first draft.",
    checkpoints: [],
    report: "draft_report_v1.md",
  },
  {
    key: "critic-v1",
    label: "Critic Pass (v1)",
    agent: "critic",
    description: "Blind review for unlinked claims, forbidden expressions, hedging compliance. Never touches ground truth.",
    checkpoints: [],
    contracts: ["critic_result.json"],
  },
  {
    key: "evaluation-v1",
    label: "Evaluation",
    agent: "evaluation",
    aliases: ["evaluation"],
    description: "The sole stage permitted to read ground truth, and only after human review.",
    checkpoints: [],
    contracts: ["evaluation_result.json"],
    reviewVersion: "v1",
  },
];

export const PHASE_2 = [
  {
    key: "denial-validation",
    label: "Denial Validation",
    agent: "denial-validation",
    description: "Validates the insurer's denial reasons against case evidence, then generates rebuttal points.",
    checkpoints: [
      { key: "retrieval-validation", label: "Evidence retrieval + validation" },
      { key: "rebuttal-generation", label: "Rebuttal point generation" },
    ],
    contracts: ["denial_validation_result.json"],
    report: "rebuttal_points.md",
  },
  {
    key: "draft-report-v2",
    label: "Draft Report (v2)",
    agent: "draft-report",
    description: "Second checkpoint of the same agent as Phase 1 -- incorporates rebuttal points.",
    checkpoints: [],
    report: "draft_report_v2.md",
  },
  {
    key: "critic-v2",
    label: "Critic Pass (v2)",
    agent: "critic",
    description: "Same agent as Phase 1, run again on the updated draft.",
    checkpoints: [],
  },
  {
    key: "evaluation-v2",
    label: "Evaluation",
    agent: "evaluation",
    description: "Same agent as Phase 1, run again.",
    checkpoints: [],
    reviewVersion: "v2",
  },
];

// Dependency-triggered, not phase-gated (see pipeline.md): runs whenever a
// flagged insurer-response document's processed text exists. Listed in its
// own group so the viewer doesn't silently omit it from the case's status.
export const TRIGGERED = [
  {
    key: "denial-response",
    label: "Denial Reason Extraction",
    agent: "denial-response",
    aliases: ["denial_response"],
    description:
      "Extracts and classifies insurer denial/reduction reasons and matches them to policy clauses. Dependency-triggered -- runs whenever a flagged insurer-response document is processed, not at a fixed phase position.",
    checkpoints: [],
    contracts: ["denial_reason_result.json"],
  },
];

export const ALL_STAGES = [...PHASE_1, ...PHASE_2, ...TRIGGERED];

// Run-state stage names are written by the orchestrator/tools and don't
// follow one registry (e.g. run_checkpoint1.py writes "document_processing"
// while this viewer keys the stage "document-pipeline") -- match on the
// stage key, its declared aliases, and a separator-insensitive comparison,
// so a real failed/passed entry is never silently rendered as "pending"
// just because of a naming-convention mismatch.
function normalizeStageName(name) {
  return String(name).toLowerCase().replace(/[-_\s]/g, "");
}

export function findRunStateEntry(runState, stageDefOrKey) {
  if (!runState?.stages) return null;
  const def = typeof stageDefOrKey === "string" ? { key: stageDefOrKey } : stageDefOrKey;
  const accepted = new Set([def.key, ...(def.aliases || [])].map(normalizeStageName));
  return runState.stages.find((s) => accepted.has(normalizeStageName(s.stage_name))) || null;
}
