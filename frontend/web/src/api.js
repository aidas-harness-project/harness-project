import { STATIC_CASES } from "./staticCaseData";

// STATIC_MODE: build with `npm run build:static` for a self-contained,
// backend-free snapshot (presentation fallback -- no live loading, no
// validation round-trips, guaranteed to render since the data is baked in
// at build time rather than fetched). Every call below still returns a
// Promise so no component needs to know which mode it's in.
const STATIC_MODE = import.meta.env.VITE_STATIC_MODE === "true";
const BASE = "http://127.0.0.1:8000";
const MEDICAL_TOKEN_KEY = "aidas.medicalOperatorToken";

function medicalToken() {
  return globalThis.sessionStorage?.getItem(MEDICAL_TOKEN_KEY) || "";
}

function setMedicalToken(token) {
  const value = token.trim();
  if (value) globalThis.sessionStorage?.setItem(MEDICAL_TOKEN_KEY, value);
  else globalThis.sessionStorage?.removeItem(MEDICAL_TOKEN_KEY);
}

async function medicalRequest(path, { method = "GET", body } = {}) {
  const token = medicalToken();
  if (!token) throw new Error("Enter an authenticated medical operator token.");
  const res = await fetch(`${BASE}${path}`, {
    method,
    body: body === undefined ? undefined : JSON.stringify(body),
    headers: {
      Authorization: `Bearer ${token}`,
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
    },
  });
  if (!res.ok) {
    const errBody = await res.json().catch(() => ({}));
    throw new Error(errBody.detail || `${res.status} ${res.statusText}`);
  }
  return res.json();
}

async function get(path) {
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `${res.status} ${res.statusText}`);
  }
  return res.json();
}

async function post(path, { body, isForm } = {}) {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    body: isForm ? body : body ? JSON.stringify(body) : undefined,
    headers: isForm ? undefined : body ? { "Content-Type": "application/json" } : undefined,
  });
  if (!res.ok) {
    const errBody = await res.json().catch(() => ({}));
    throw new Error(errBody.detail || `${res.status} ${res.statusText}`);
  }
  return res.json();
}

const liveApi = {
  medicalWorkspaceAvailable: true,
  mutationsAvailable: true,
  listCases: () => get("/api/cases"),
  runState: (caseId) => get(`/api/cases/${caseId}/run-state`),
  ledgers: (caseId) => get(`/api/cases/${caseId}/ledgers`),
  contract: (caseId, name) => get(`/api/cases/${caseId}/contract/${name}`),
  report: (caseId, name) => get(`/api/cases/${caseId}/report/${name}`),

  uploadDocuments: (caseId, files) => {
    const form = new FormData();
    for (const f of files) form.append("files", f);
    return post(`/api/upload?case_id=${encodeURIComponent(caseId)}`, { body: form, isForm: true });
  },
  runCase: (caseId) => post(`/api/cases/${caseId}/run`),
  runStatus: (caseId) => get(`/api/cases/${caseId}/run-status`),

  setLedgerStatus: (caseId, fileName, status, reviewer, reason, operationId) =>
    post(`/api/cases/${caseId}/ledger/status`, { body: { file_name: fileName, status, reviewer, reason, operation_id: operationId } }),
  setConflictVerdict: (caseId, conflictId, verdict, note, operationId) =>
    post(`/api/cases/${caseId}/conflicts/${conflictId}/verdict`, { body: { verdict, note, operation_id: operationId } }),

  // In-UI review of the actual object under decision + P8/D1 human gates.
  sourceFileUrl: (caseId, fileName) =>
    `${BASE}/api/cases/${caseId}/source-file?name=${encodeURIComponent(fileName)}`,
  ocrReview: (caseId) => get(`/api/cases/${caseId}/ocr-review`),
  ocrResolve: (caseId, docId, page, chosenReading, reviewer, note) =>
    post(`/api/cases/${caseId}/ocr-resolve`, {
      body: { doc_id: docId, page, chosen_reading: chosenReading, reviewer, note },
    }),
  humanReview: (caseId) => get(`/api/cases/${caseId}/human-review`),
  markHumanReviewComplete: (caseId, version, reviewer) =>
    post(`/api/cases/${caseId}/human-review-complete`, { body: { version, reviewer } }),

  hasMedicalToken: () => Boolean(medicalToken()),
  setMedicalToken,
  medicalVariables: (caseId, revisionSha) => medicalRequest(
    `/api/cases/${caseId}/medical-variables${
      revisionSha ? `?revision_sha=${encodeURIComponent(revisionSha)}` : ""
    }`,
  ),
  medicalReviews: (caseId) => medicalRequest(`/api/cases/${caseId}/medical-reviews`),
  medicalReviewAction: (caseId, reviewItemId, action, body, operationId) => medicalRequest(
    `/api/cases/${caseId}/medical-reviews/${reviewItemId}/actions/${action}`,
    { method: "POST", body: { ...body, operation_id: operationId } },
  ),
  medicalEvidence: (
    caseId, reviewItemId, requestId, requestVersion, locatorId,
  ) => medicalRequest(
    `/api/cases/${caseId}/medical-reviews/${reviewItemId}`
      + `/requests/${requestId}/versions/${requestVersion}/evidence/${locatorId}`,
  ),
};

function notFound(name) {
  return Promise.reject(new Error(`${name} not in the static snapshot`));
}

function rejectStaticMutation() {
  return Promise.reject(new Error("read-only snapshot -- not connected to a live case"));
}

const staticApi = {
  medicalWorkspaceAvailable: false,
  mutationsAvailable: false,
  listCases: () => Promise.resolve(Object.keys(STATIC_CASES)),
  runState: (caseId) => Promise.resolve(STATIC_CASES[caseId]?.runState),
  ledgers: (caseId) => Promise.resolve(STATIC_CASES[caseId]?.ledgers),
  contract: (caseId, name) =>
    STATIC_CASES[caseId]?.contracts[name] ? Promise.resolve(STATIC_CASES[caseId].contracts[name]) : notFound(name),
  report: (caseId, name) =>
    STATIC_CASES[caseId]?.reports[name] ? Promise.resolve(STATIC_CASES[caseId].reports[name]) : notFound(name),

  // Static snapshots are read-only: every mutation rejects and the
  // corresponding controls are omitted from the rendered application.
  uploadDocuments: rejectStaticMutation,
  runCase: rejectStaticMutation,
  runStatus: () => Promise.resolve({ status: "unavailable" }),
  setLedgerStatus: rejectStaticMutation,
  setConflictVerdict: rejectStaticMutation,
  sourceFileUrl: () => null,
  ocrReview: () => Promise.resolve({ documents: [] }),
  ocrResolve: rejectStaticMutation,
  humanReview: () => Promise.resolve({}),
  markHumanReviewComplete: rejectStaticMutation,
  hasMedicalToken: () => false,
  setMedicalToken: () => {},
  medicalVariables: () => notFound("medical variables"),
  medicalReviews: () => notFound("medical reviews"),
  medicalReviewAction: rejectStaticMutation,
  medicalEvidence: () => notFound("medical evidence"),
};

export const api = STATIC_MODE ? staticApi : liveApi;
