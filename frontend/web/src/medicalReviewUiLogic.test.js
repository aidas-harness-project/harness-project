import test from "node:test";
import assert from "node:assert/strict";

import { buildMedicalWorkspace, caseSnapshotFor, currentRequestRevision, evidenceMatchesCoordinate, medicalOperationSignature, planActionCompletion } from "./medicalReviewUiLogic.js";

const observation = (id, value, date, locator) => ({
  observation_id: id,
  value_state: "asserted",
  value_type: "text",
  text_value: value,
  observed_at: { precision: "day", date },
  evidence: [{ locator_id: locator, document_id: "DOC_001", page: 1, quote: value }],
  extraction_method: "model_extracted",
});

test("buildMedicalWorkspace keeps canonical timeline and balanced issue evidence visible", () => {
  const variables = {
    medical_issues: [{
      issue_id: "MCI_0001",
      question: "Is the later finding consistent with the earlier record?",
      evidence_links: [
        {
          issue_evidence_link_id: "MIEL_0001",
          observation_id: "MO_0002",
          relation: "supports",
          materiality: "core",
          reason: "Later record supports the finding.",
        },
        {
          issue_evidence_link_id: "MIEL_0002",
          observation_id: "MO_0001",
          relation: "conflicts",
          materiality: "core",
          reason: "Earlier record documents the opposite finding.",
        },
      ],
    }],
    variables: [
      {
        variable_id: "MV_0002",
        label: "Later examination",
        observations: [observation("MO_0002", "Positive finding", "2026-02-01", "MEV_0002")],
      },
      {
        variable_id: "MV_0001",
        label: "Initial examination",
        observations: [observation("MO_0001", "Negative finding", "2026-01-01", "MEV_0001")],
      },
    ],
    timeline_observation_ids: ["MO_0001", "MO_0002"],
    contradiction_groups: [{
      contradiction_group_id: "MCG_0001",
      observation_ids: ["MO_0001", "MO_0002"],
      conflict_id: "CONFLICT_1",
    }],
  };
  const ledger = {
    review_items: [{
      review_item_id: "MRI_0001",
      issue_id: "MCI_0001",
      state: "answered",
      current_request_id: "MRR_0001",
      source_reinspection_records: [{
        source_reinspection_id: "MRI_0001-S01",
        result: "resolved",
      }],
      decisions: [{
        decision_id: "MRD_000001",
        decision_version: 1,
        decision: "refer",
        rationale: "Material contradiction requires focused medical interpretation.",
      }],
      requests: [{
        request_id: "MRR_0001",
        current_response_id: "MRR_0001-R01",
        versions: [{
          request_id: "MRR_0001",
          request_version: 1,
          medical_variables_revision: { sha256: "a".repeat(64) },
          question: "Reconcile the two examination findings.",
          suggested_specialty_code: "orthopedics",
          included_evidence_locator_ids: ["MEV_0001", "MEV_0002"],
          uncertainties: ["The source does not explain the change."],
          source_coverage: ["One eligible source remains unavailable."],
        }],
        assignments: [{ assignment_id: "MRR_0001-A01" }],
        responses: [{
          response_id: "MRR_0001-R01",
          response_version: 1,
          interpretation: "The records can reflect interval change.",
          basis: "Both dated examinations were reviewed.",
          uncertainty: "No intervening examination is available.",
          alternative_interpretations: ["Documentation variance remains possible."],
          additional_evidence_needed: [],
          response_status: "completed",
        }],
      }],
    }],
    wait_episodes: [{
      human_input_id: "MRH_000001",
      input_kind: "expert_response",
      review_item_id: "MRI_0001",
      related_review_item_ids: [],
      status: "received",
      resolution: "response submitted",
    }],
  };

  const runState = {
    human_input_status: [{
      human_input_id: "MRH_000001",
      status: "waiting",
    }],
  };
  const workspace = buildMedicalWorkspace(variables, ledger, null, runState);

  assert.deepEqual(
    workspace.timeline.map((entry) => entry.observation_id),
    ["MO_0001", "MO_0002"],
  );
  assert.deepEqual(
    workspace.conflicts[0].observations.map((entry) => entry.text_value),
    ["Negative finding", "Positive finding"],
  );
  assert.equal(workspace.referral.decision, "refer");
  assert.match(workspace.referral.rationale, /focused medical interpretation/);
  assert.equal(workspace.request.question, "Reconcile the two examination findings.");
  assert.equal(workspace.response.interpretation, "The records can reflect interval change.");
  assert.deepEqual(workspace.waits.map((wait) => wait.status), ["waiting"]);
  assert.equal(workspace.waits[0].ledger_status, "received");
  assert.deepEqual(workspace.waitProjectionErrors, []);
  assert.deepEqual(workspace.assignmentHistory.map((row) => row.assignment_id), ["MRR_0001-A01"]);
  assert.deepEqual(
    workspace.sourceReinspectionHistory.map((row) => row.source_reinspection_id),
    ["MRI_0001-S01"],
  );
  assert.equal(currentRequestRevision(ledger, "MRI_0001"), "a".repeat(64));
  const coordinate = {
    caseId: "CASE_9001",
    reviewItemId: "MRI_0001",
    requestId: "MRR_0001",
    requestVersion: 1,
    revisionSha: "a".repeat(64),
    locatorId: "MEV_0001",
  };
  assert.equal(evidenceMatchesCoordinate({
    case_id: "CASE_9001",
    review_item_id: "MRI_0001",
    request_id: "MRR_0001",
    request_version: 1,
    revision_sha: "a".repeat(64),
    locator: { locator_id: "MEV_0001" },
  }, coordinate), true);
  assert.equal(evidenceMatchesCoordinate({
    case_id: "CASE_9001",
    review_item_id: "MRI_0001",
    request_id: "MRR_0001",
    request_version: 2,
    revision_sha: "a".repeat(64),
    locator: { locator_id: "MEV_0001" },
  }, coordinate), false);
  assert.deepEqual(
    workspace.evidence.relevant.map((entry) => entry.observation_id),
    ["MO_0002"],
  );
  assert.deepEqual(
    workspace.evidence.countervailing.map((entry) => entry.observation_id),
    ["MO_0001"],
  );
});

test("buildMedicalWorkspace selects an explicit review item and its event history", () => {
  const variables = {
    variables: [],
    medical_issues: [
      { issue_id: "MI_0001", evidence_links: [] },
      { issue_id: "MI_0002", evidence_links: [] },
    ],
  };
  const ledger = {
    review_items: [
      { review_item_id: "MRI_0001", issue_id: "MI_0001", requests: [], decisions: [] },
      { review_item_id: "MRI_0002", issue_id: "MI_0002", requests: [], decisions: [] },
    ],
    events: [
      { event_id: "MRE_000001", review_item_id: "MRI_0001", action: "assign" },
      { event_id: "MRE_000002", review_item_id: "MRI_0002", action: "close" },
    ],
    wait_episodes: [
      { human_input_id: "MRH_000001", review_item_id: "MRI_0001", related_review_item_ids: [], status: "waiting" },
      { human_input_id: "MRH_000002", review_item_id: "MRI_0002", related_review_item_ids: [], status: "received" },
    ],
  };
  const runState = { human_input_status: [
    { human_input_id: "MRH_000001", status: "waiting" },
    { human_input_id: "MRH_000002", status: "received" },
  ] };
  const workspace = buildMedicalWorkspace(variables, ledger, "MRI_0002", runState);
  assert.equal(workspace.item.review_item_id, "MRI_0002");
  assert.deepEqual(
    workspace.items.map((item) => item.review_item_id),
    ["MRI_0001", "MRI_0002"],
  );
  assert.deepEqual(workspace.events.map((event) => event.event_id), ["MRE_000002"]);
  assert.deepEqual(workspace.waitProjectionErrors, []);
});

test("workspace histories are complete and balanced evidence stays request pinned", () => {
  const variables = {
    medical_issues: [{
      issue_id: "MCI_0001",
      evidence_links: [
        { observation_id: "MO_0001", relation: "supports", reason: "Pinned" },
        { observation_id: "MO_0002", relation: "conflicts", reason: "Newer only" },
      ],
    }],
    variables: [{
      variable_id: "MV_0001",
      label: "Finding",
      observations: [
        observation("MO_0001", "Pinned finding", "2026-01-01", "MEV_PACKAGE"),
        observation("MO_0002", "New finding", "2026-02-01", "MEV_NEW"),
      ],
    }],
  };
  const ledger = { review_items: [{
    review_item_id: "MRI_0001",
    issue_id: "MCI_0001",
    current_request_id: "MRR_0001",
    decisions: [],
    requests: [{
      request_id: "MRR_0001",
      current_response_id: "MRR_0001-R02",
      versions: [
        { request_id: "MRR_0001", request_version: 1, included_evidence_locator_ids: ["MEV_PACKAGE"] },
        { request_id: "MRR_0001", request_version: 2, included_evidence_locator_ids: ["MEV_PACKAGE"] },
      ],
      responses: [
        { response_id: "MRR_0001-R01", response_version: 1 },
        { response_id: "MRR_0001-R02", response_version: 2 },
      ],
    }],
  }] };

  const workspace = buildMedicalWorkspace(variables, ledger);
  assert.deepEqual(workspace.requestHistory.map((row) => row.request_version), [1, 2]);
  assert.deepEqual(workspace.responseHistory.map((row) => row.response_version), [1, 2]);
  assert.deepEqual(
    workspace.evidence.relevant.flatMap((row) => row.evidence.map((entry) => entry.locator_id)),
    ["MEV_PACKAGE"],
  );
  assert.equal(workspace.evidence.countervailing.length, 0);
});

test("case snapshots never expose previous-case state under a new case id", () => {
  const snapshot = { caseId: "CASE_0001", runState: { run_id: "RUN_1" } };
  assert.equal(caseSnapshotFor(snapshot, "CASE_0002"), null);
  assert.equal(caseSnapshotFor(snapshot, "CASE_0001"), snapshot);
});

test("medical operation signature changes with the canonical lifecycle head", () => {
  const body = { reason: "Same disposition" };
  const first = medicalOperationSignature("close", body, [{ event_id: "MRE_000001" }]);
  const retry = medicalOperationSignature("close", body, [{ event_id: "MRE_000001" }]);
  const laterCycle = medicalOperationSignature("close", body, [{ event_id: "MRE_000002" }]);
  assert.equal(retry, first);
  assert.notEqual(laterCycle, first);
});

test("stale action completion reconciles canonical state without retaining busy ownership", () => {
  assert.deepEqual(planActionCompletion(3, 4, "committed"), {
    isCurrent: false,
    releaseBusy: false,
    reconcile: true,
    projectionPending: false,
  });
  assert.deepEqual(planActionCompletion(3, 4, "failed"), {
    isCurrent: false,
    releaseBusy: false,
    reconcile: false,
    projectionPending: false,
  });
  assert.deepEqual(planActionCompletion(4, 4, "committed_projection_pending"), {
    isCurrent: true,
    releaseBusy: true,
    reconcile: false,
    projectionPending: true,
  });
  assert.deepEqual(planActionCompletion(3, 4, "committed_projection_pending"), {
    isCurrent: false,
    releaseBusy: false,
    reconcile: true,
    projectionPending: true,
  });
});
