// Static structure of the pipeline -- mirrors pipeline.md. The dynamic part
// (status, timestamps, attempt counts) comes from the backend's run-state;
// this file only describes the shape that never changes between cases.

export const PHASE_1 = [
  {
    key: "case-intake",
    label: "Case Intake",
    agent: null,
    description: "Splits the source case into model input and ground truth, gated by a human-approved, per-file ledger.",
    checkpoints: [],
    gate: "source-ledger",
  },
  {
    key: "document-pipeline",
    label: "Document Processing",
    agent: "document-pipeline",
    description: "OCR with dual-path cross-validation, classification, redaction, chunking.",
    checkpoints: [
      { key: "ocr-crossval-classify", label: "OCR + cross-validation + classification" },
      { key: "redaction", label: "Redaction" },
      { key: "chunking", label: "Chunking" },
    ],
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
    description: "The sole stage permitted to read ground truth, and only after human review.",
    checkpoints: [],
    contracts: ["evaluation_result.json"],
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
  },
];

export const ALL_STAGES = [...PHASE_1, ...PHASE_2];

export function findRunStateEntry(runState, stageKey) {
  if (!runState?.stages) return null;
  return runState.stages.find((s) => s.stage_name === stageKey) || null;
}
