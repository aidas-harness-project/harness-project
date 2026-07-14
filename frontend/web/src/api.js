import { STATIC_CASES } from "./staticCaseData";

// STATIC_MODE: build with `npm run build:static` for a self-contained,
// backend-free snapshot (presentation fallback -- no live loading, no
// validation round-trips, guaranteed to render since the data is baked in
// at build time rather than fetched). Every call below still returns a
// Promise so no component needs to know which mode it's in.
const STATIC_MODE = import.meta.env.VITE_STATIC_MODE === "true";
const BASE = "http://127.0.0.1:8000";

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

  setLedgerStatus: (caseId, fileName, status, reviewer, reason) =>
    post(`/api/cases/${caseId}/ledger/status`, { body: { file_name: fileName, status, reviewer, reason } }),
  setConflictVerdict: (caseId, conflictId, verdict, note) =>
    post(`/api/cases/${caseId}/conflicts/${conflictId}/verdict`, { body: { verdict, note } }),

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
};

function notFound(name) {
  return Promise.reject(new Error(`${name} not in the static snapshot`));
}

const staticApi = {
  listCases: () => Promise.resolve(Object.keys(STATIC_CASES)),
  runState: (caseId) => Promise.resolve(STATIC_CASES[caseId]?.runState),
  ledgers: (caseId) => Promise.resolve(STATIC_CASES[caseId]?.ledgers),
  contract: (caseId, name) =>
    STATIC_CASES[caseId]?.contracts[name] ? Promise.resolve(STATIC_CASES[caseId].contracts[name]) : notFound(name),
  report: (caseId, name) =>
    STATIC_CASES[caseId]?.reports[name] ? Promise.resolve(STATIC_CASES[caseId].reports[name]) : notFound(name),

  // Audit-write and run-launch actions are inert in static mode -- this
  // build has no backend to actually call. Resolve harmlessly rather than
  // erroring, since the buttons are still visible in the snapshot.
  uploadDocuments: () => Promise.resolve({ files: [] }),
  runCase: () => Promise.resolve({ status: "unavailable" }),
  runStatus: () => Promise.resolve({ status: "unavailable" }),
  setLedgerStatus: () => Promise.reject(new Error("read-only snapshot -- not connected to a live case")),
  setConflictVerdict: () => Promise.reject(new Error("read-only snapshot -- not connected to a live case")),
  sourceFileUrl: () => null,
  ocrReview: () => Promise.resolve({ documents: [] }),
  ocrResolve: () => Promise.reject(new Error("read-only snapshot -- not connected to a live case")),
  humanReview: () => Promise.resolve({}),
  markHumanReviewComplete: () => Promise.reject(new Error("read-only snapshot -- not connected to a live case")),
};

export const api = STATIC_MODE ? staticApi : liveApi;
