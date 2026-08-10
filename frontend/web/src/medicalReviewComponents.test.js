import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const source = (name) => readFile(new URL(name, import.meta.url), "utf8");

test("medical workspace renders the approved purpose-built surface", async () => {
  const [variables, review, reviewPackage, responseForm, stageDetail, api, app] = await Promise.all([
    source("./components/MedicalVariablesPanel.jsx"),
    source("./components/MedicalReviewPanel.jsx"),
    source("./components/MedicalReviewPackage.jsx"),
    source("./components/MedicalReviewResponseForm.jsx"),
    source("./components/StageDetail.jsx"),
    source("./api.js"),
    source("./App.jsx"),
  ]);

  for (const label of [
    "Medical timeline",
    "Contradictions",
    "Relevant evidence",
    "Countervailing evidence",
  ]) {
    assert.match(variables, new RegExp(label));
  }
  for (const label of [
    "Referral status",
    "Referral rationale",
    "Request for human medical interpretation, not an adverse finding",
    "Complete event history",
    "Expert input status",
    "Localhost-only",
    "Medical response",
  ]) {
    assert.match(review, new RegExp(label));
  }
  assert.match(reviewPackage, /Request version/);
  assert.match(reviewPackage, /Focused request/);
  assert.match(reviewPackage, /Evidence package/);
  assert.match(reviewPackage, /Source coverage limitations/);
  assert.match(reviewPackage, /onOpenEvidence/);
  assert.match(reviewPackage, /assignmentHistory/);
  assert.match(reviewPackage, /sourceReinspectionHistory/);
  assert.match(reviewPackage, /StructuredMedicalRecord value=\{entry\}/);
  assert.match(review, /useRef/);
  assert.match(review, /actionGeneration/);
  assert.match(review, /generation !== actionGeneration\.current/);
  assert.match(review, /setBusy\(false\)/);
  assert.match(review, /setReconcileGeneration/);
  assert.match(review, /medical-warning/);
  assert.match(review, /event\.actor\.declared_role/);
  assert.match(review, /currentRequestRevision/);
  assert.match(review, /evidenceMatchesCoordinate/);
  assert.match(review, /Reviewed request \/ assignment/);
  assert.match(review, /Reviewed evidence locators/);
  assert.match(reviewPackage, /function StructuredMedicalRecord/);
  assert.match(reviewPackage, /Object\.entries\(value\)/);
  assert.match(reviewPackage, /Complete canonical request record/);
  assert.match(reviewPackage, /Complete canonical assignment record/);
  assert.match(reviewPackage, /Complete canonical response record/);
  assert.match(reviewPackage, /Complete canonical reinspection record/);
  assert.match(reviewPackage, /Complete canonical evidence record/);
  assert.match(review, /Complete canonical wait episode/);
  assert.match(review, /Complete canonical lifecycle event and role policy snapshot/);

  for (const field of [
    "Interpretation",
    "Basis",
    "Uncertainty",
    "Attestation",
    "Additional evidence needed",
  ]) {
    assert.match(responseForm, new RegExp(field));
  }
  assert.doesNotMatch(responseForm, /Raw JSON|JSON editor|JSON\.stringify\(form/);
  assert.doesNotMatch(responseForm, /new Date\(\)\.toISOString/);
  assert.doesNotMatch(responseForm, /evidence_locator_ids_reviewed:\s*request\.included_evidence_locator_ids/);
  assert.doesNotMatch(responseForm, /assigningActorId/);
  assert.match(responseForm, /Evidence locator IDs actually reviewed/);
  assert.match(responseForm, /Source reinspection confirmed/);
  assert.match(responseForm, /No-verdict boundary confirmed/);
  assert.match(responseForm, /MEDICAL_REVIEW_ACTIONS/);

  assert.match(review, /setVariables\(null\)/);
  assert.match(review, /setLedger\(null\)/);
  assert.match(reviewPackage, /evidence\.locator\?\.locator_id/);
  assert.match(reviewPackage, /Request and response history/);
  assert.match(reviewPackage, /Canonical history remains available below/);
  assert.doesNotMatch(reviewPackage, /if \(!request\) return/);
  assert.match(app, /caseSnapshotFor/);
  assert.match(app, /generationRef/);
  assert.match(app, /generation !== generationRef\.current/);
  assert.match(app, /key=\{`\$\{current\}:\$\{stageDef\.key\}`\}/);
  assert.match(stageDetail, /MedicalReviewPanel key=\{caseId\}/);

  assert.match(stageDetail, /MedicalReviewPanel/);
  assert.match(stageDetail, /stageDef\.key === "claim-analysis"/);
  assert.match(api, /Authorization:\s*`Bearer/);
  assert.match(api, /medicalWorkspaceAvailable/);
  assert.match(review, /Medical workspace is unavailable in this static snapshot/);
  assert.match(api, /\/medical-variables/);
  assert.match(api, /\/medical-reviews/);
  assert.match(api, /\/actions\/\$\{action\}/);
  assert.match(api, /\/versions\/\$\{requestVersion\}\/evidence/);
  assert.match(api, /operation_id/);
  assert.match(review, /durableOperationId/);
  assert.match(review, /clearDurableOperation/);
  assert.match(review, /api\.runState/);
  assert.match(review, /waitProjectionErrors/);
});

test("static snapshot rejects mutations and omits every mutation control", async () => {
  const [api, app, sidebar, stageDetail, ledger, ocr, human] = await Promise.all([
    source("./api.js"),
    source("./App.jsx"),
    source("./components/Sidebar.jsx"),
    source("./components/StageDetail.jsx"),
    source("./components/LedgerPanel.jsx"),
    source("./components/OcrReviewPanel.jsx"),
    source("./components/HumanReviewPanel.jsx"),
  ]);

  assert.match(api, /mutationsAvailable: false/);
  assert.match(api, /uploadDocuments: rejectStaticMutation/);
  assert.match(api, /runCase: rejectStaticMutation/);
  assert.match(api, /setLedgerStatus: rejectStaticMutation/);
  assert.match(api, /setConflictVerdict: rejectStaticMutation/);
  assert.match(ledger, /durableOperationId/);
  assert.match(ledger, /clearDurableOperation/);
  assert.match(api, /ocrResolve: rejectStaticMutation/);
  assert.match(api, /markHumanReviewComplete: rejectStaticMutation/);
  assert.match(api, /medicalReviewAction: rejectStaticMutation/);
  assert.doesNotMatch(api, /uploadDocuments: \(\) => Promise\.resolve/);
  assert.doesNotMatch(api, /runCase: \(\) => Promise\.resolve/);

  assert.match(app, /mutationsAvailable=\{api\.mutationsAvailable\}/);
  assert.match(app, /readOnly=\{!api\.mutationsAvailable\}/);
  assert.match(sidebar, /mutationsAvailable &&/);
  assert.match(stageDetail, /readOnly=\{readOnly\}/g);
  assert.match(ledger, /!readOnly &&/g);
  assert.match(ocr, /!readOnly &&/g);
  assert.match(human, /!readOnly && !state\.review_complete/);
});
