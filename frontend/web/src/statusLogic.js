import { findRunStateEntry } from "./pipelineDefinition";

// Derives what a stage is ACTUALLY doing, cross-referencing run-state against
// the ledgers and the P8 OCR-review queue -- a stage can show "pending" in
// run-state while really being blocked on a human decision recorded
// elsewhere. Every halt condition here traces to a specific
// harness-guardrails rule; nothing is inferred without a concrete field
// backing it.
export function deriveStageStatus(stageDef, runState, ledgers, ocrReview) {
  const entry = findRunStateEntry(runState, stageDef);
  const base = entry?.status || "pending";

  if (stageDef.gate === "source-ledger") {
    const files = ledgers?.source_ledger?.files || [];
    const rejected = files.filter((f) => f.review_status === "rejected");
    const pending = files.filter((f) => f.review_status === "pending");
    if (rejected.length) {
      return {
        status: "paused",
        rule: "D2",
        reason: `${rejected.length} file(s) rejected -- the entire case is blocked until resolved`,
        detail: rejected,
      };
    }
    if (pending.length) {
      return {
        status: "paused",
        rule: "D2",
        reason: `${pending.length} file(s) awaiting human review before intake can proceed`,
        detail: pending,
      };
    }
  }

  if (stageDef.gate === "ocr-review") {
    const blocked = ocrReview?.documents || [];
    if (blocked.length) {
      const pageCount = blocked.reduce((n, d) => n + d.pages.length, 0);
      return {
        status: "paused",
        rule: "P8",
        reason: `${pageCount} page(s) across ${blocked.length} document(s) have disagreeing dual reads -- human must pick the correct reading below`,
        detail: blocked,
      };
    }
  }

  if (stageDef.gate === "conflict-ledger") {
    const conflicts = ledgers?.conflict_ledger?.conflicts || [];
    const pending = conflicts.filter((c) => c.verdict === "pending");
    if (pending.length) {
      return {
        status: "paused",
        rule: "P6",
        reason: `${pending.length} conflict(s) pending resolution`,
        detail: pending,
      };
    }
  }

  const acceptedNames = new Set(
    [stageDef.key, ...(stageDef.aliases || [])].map((n) => String(n).toLowerCase().replace(/[-_\s]/g, ""))
  );
  const humanWait = runState?.human_input_status?.find(
    (h) => acceptedNames.has(String(h.stage_name).toLowerCase().replace(/[-_\s]/g, "")) && h.status === "waiting"
  );
  if (humanWait) {
    return { status: "paused", rule: "P7", reason: humanWait.description, detail: humanWait };
  }

  if (base === "failed" && (entry?.attempt_count ?? 0) >= 3) {
    return {
      status: "halted",
      rule: "P9",
      reason: `Failed after ${entry.attempt_count} attempts -- halted for user audit`,
      detail: entry,
    };
  }

  return { status: base, rule: null, reason: null, detail: entry };
}

export const STATUS_META = {
  passed: { label: "Passed", color: "var(--sage)" },
  failed: { label: "Failed", color: "var(--oxblood)" },
  halted: { label: "Halted", color: "var(--oxblood)" },
  paused: { label: "Paused", color: "var(--gold-bright)" },
  in_progress: { label: "In progress", color: "var(--slate)" },
  pending: { label: "Pending", color: "var(--parchment-faint)" },
};
