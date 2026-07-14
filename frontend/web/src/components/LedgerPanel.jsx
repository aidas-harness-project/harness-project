import { useState } from "react";
import { api } from "../api";

const REVIEW_COLOR = {
  approved: "var(--sage)",
  rejected: "var(--oxblood)",
  pending: "var(--gold-bright)",
};

const VERDICT_COLOR = {
  resolved: "var(--sage)",
  false_positive: "var(--slate)",
  pending: "var(--gold-bright)",
};

export function useReviewerName() {
  const [reviewer, setReviewer] = useState(() => localStorage.getItem("reviewerName") || "");
  const update = (v) => {
    setReviewer(v);
    localStorage.setItem("reviewerName", v);
  };
  return [reviewer, update];
}

export function SourceLedgerPanel({ ledger, caseId, onChanged }) {
  const [reviewer, setReviewer] = useReviewerName();
  const [rejecting, setRejecting] = useState(null); // file_name currently entering a reason
  const [reasonText, setReasonText] = useState("");
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState(null);
  const [viewing, setViewing] = useState(null); // file_name shown in the inline viewer

  if (!ledger) return <p className="muted">No source ledger for this case.</p>;

  async function approve(fileName) {
    if (!reviewer.trim()) return setError("enter a reviewer name first");
    setBusy(fileName);
    setError(null);
    try {
      await api.setLedgerStatus(caseId, fileName, "approved", reviewer);
      onChanged?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(null);
    }
  }

  async function confirmReject(fileName) {
    if (!reviewer.trim()) return setError("enter a reviewer name first");
    if (!reasonText.trim()) return setError("a rejection reason is required");
    setBusy(fileName);
    setError(null);
    try {
      await api.setLedgerStatus(caseId, fileName, "rejected", reviewer, reasonText);
      setRejecting(null);
      setReasonText("");
      onChanged?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="ledger-panel">
      <label className="reviewer-field">
        Reviewer name
        <input value={reviewer} onChange={(e) => setReviewer(e.target.value)} placeholder="e.g. 김태윤" />
      </label>
      {error && <p className="audit-error">{error}</p>}
      <table>
        <thead>
          <tr>
            <th>File</th>
            <th>Classification</th>
            <th>Review status</th>
            <th>Reviewer</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody>
          {ledger.files.map((f) => (
            <tr key={f.file_name}>
              <td className="mono">
                {f.file_name}
                <div className="file-actions">
                  <button
                    className="btn-tiny"
                    onClick={() => setViewing(viewing === f.file_name ? null : f.file_name)}
                  >
                    {viewing === f.file_name ? "hide document" : "view document"}
                  </button>
                  {api.sourceFileUrl(caseId, f.file_name) && (
                    <a
                      className="btn-tiny open-link"
                      href={api.sourceFileUrl(caseId, f.file_name)}
                      target="_blank"
                      rel="noreferrer"
                    >
                      open ↗
                    </a>
                  )}
                </div>
                {f.content_warning && (
                  <div className="content-warning">⚠ {f.content_warning}</div>
                )}
              </td>
              <td>{f.classification}</td>
              <td style={{ color: REVIEW_COLOR[f.review_status] }}>
                {f.review_status}
                {f.rejection_reason && <div className="reason">{f.rejection_reason}</div>}
              </td>
              <td className="muted">{f.reviewed_by || "--"}</td>
              <td>
                {f.review_status === "pending" &&
                  (rejecting === f.file_name ? (
                    <div className="reject-inline">
                      <input
                        placeholder="why is this wrong?"
                        value={reasonText}
                        onChange={(e) => setReasonText(e.target.value)}
                        autoFocus
                      />
                      <button className="btn-tiny confirm" onClick={() => confirmReject(f.file_name)} disabled={busy === f.file_name}>
                        confirm
                      </button>
                      <button className="btn-tiny" onClick={() => setRejecting(null)}>
                        cancel
                      </button>
                    </div>
                  ) : (
                    <div className="action-row">
                      <button className="btn-tiny approve" onClick={() => approve(f.file_name)} disabled={busy === f.file_name}>
                        approve
                      </button>
                      <button className="btn-tiny reject" onClick={() => setRejecting(f.file_name)} disabled={busy === f.file_name}>
                        reject
                      </button>
                    </div>
                  ))}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {viewing && api.sourceFileUrl(caseId, viewing) && (
        <div className="source-viewer">
          <div className="source-viewer-head mono">{viewing}</div>
          <object
            data={api.sourceFileUrl(caseId, viewing)}
            type={viewing.toLowerCase().endsWith(".pdf") ? "application/pdf" : undefined}
            aria-label={`source document ${viewing}`}
          >
            <p className="muted">
              Couldn't embed this document —{" "}
              <a href={api.sourceFileUrl(caseId, viewing)} target="_blank" rel="noreferrer">
                open it in a new tab
              </a>{" "}
              instead.
            </p>
          </object>
        </div>
      )}
      <style>{ledgerStyles}</style>
    </div>
  );
}

export function ConflictLedgerPanel({ ledger, caseId, onChanged }) {
  const [reviewer] = useReviewerName();
  const [noting, setNoting] = useState(null); // {conflictId, verdict}
  const [noteText, setNoteText] = useState("");
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState(null);

  if (!ledger || !ledger.conflicts?.length) {
    return <p className="muted">No conflicts recorded for this case.</p>;
  }

  async function confirmVerdict(conflictId) {
    if (!noteText.trim()) return setError("a resolution note is required");
    setBusy(conflictId);
    setError(null);
    try {
      await api.setConflictVerdict(caseId, conflictId, noting.verdict, `${noteText}${reviewer ? ` (${reviewer})` : ""}`);
      setNoting(null);
      setNoteText("");
      onChanged?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="ledger-panel conflict-list">
      {error && <p className="audit-error">{error}</p>}
      {ledger.conflicts.map((c) => (
        <div className="conflict-card" key={c.conflict_id}>
          <div className="conflict-head">
            <span className="mono">{c.conflict_id}</span>
            <span style={{ color: VERDICT_COLOR[c.verdict] }}>{c.verdict}</span>
          </div>
          <div className="conflict-topic">{c.field_or_topic}</div>
          <div className="conflict-sources">
            {c.sources.map((s, i) => (
              <div className="source-side" key={i}>
                <span className="mono">
                  {s.document_id}
                  {s.page ? ` p.${s.page}` : ""}
                </span>
                <span className="source-value">{s.value}</span>
                <blockquote>{s.quote}</blockquote>
              </div>
            ))}
          </div>
          {c.resolution_note && <div className="resolution">Resolution: {c.resolution_note}</div>}

          {c.verdict === "pending" &&
            (noting?.conflictId === c.conflict_id ? (
              <div className="reject-inline conflict-note">
                <input
                  placeholder="resolution note -- what did you decide, and why?"
                  value={noteText}
                  onChange={(e) => setNoteText(e.target.value)}
                  autoFocus
                />
                <button className="btn-tiny confirm" onClick={() => confirmVerdict(c.conflict_id)} disabled={busy === c.conflict_id}>
                  save
                </button>
                <button className="btn-tiny" onClick={() => setNoting(null)}>
                  cancel
                </button>
              </div>
            ) : (
              <div className="action-row conflict-actions">
                <button className="btn-tiny approve" onClick={() => setNoting({ conflictId: c.conflict_id, verdict: "resolved" })}>
                  mark resolved
                </button>
                <button className="btn-tiny" onClick={() => setNoting({ conflictId: c.conflict_id, verdict: "false_positive" })}>
                  false positive
                </button>
              </div>
            ))}
        </div>
      ))}
      <style>{ledgerStyles}</style>
    </div>
  );
}

const ledgerStyles = `
  .ledger-panel table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
  .ledger-panel th { text-align: left; color: var(--parchment-faint); font-weight: 500; padding: 6px 10px; border-bottom: 1px solid var(--hairline); font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; }
  .ledger-panel td { padding: 8px 10px; border-bottom: 1px solid var(--hairline); vertical-align: top; }
  .reason { font-size: 12px; color: var(--parchment-faint); margin-top: 2px; }
  .reviewer-field { display: flex; flex-direction: column; gap: 5px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--parchment-faint); margin-bottom: 14px; max-width: 240px; }
  .reviewer-field input { background: var(--surface); border: 1px solid var(--hairline); color: var(--parchment); padding: 6px 10px; border-radius: 4px; font-family: var(--sans); font-size: 13px; text-transform: none; }
  .audit-error { color: var(--oxblood); font-size: 12.5px; margin-bottom: 10px; }
  .action-row { display: flex; gap: 6px; }
  .btn-tiny {
    font-family: var(--mono); font-size: 11px; padding: 5px 10px; border-radius: 4px; cursor: pointer;
    background: var(--surface-2); border: 1px solid var(--hairline); color: var(--parchment-dim);
  }
  .btn-tiny:disabled { opacity: 0.5; cursor: wait; }
  .btn-tiny.approve { border-color: var(--sage); color: var(--sage); }
  .btn-tiny.approve:hover { background: var(--sage); color: var(--ink); }
  .btn-tiny.reject { border-color: var(--oxblood); color: var(--oxblood); }
  .btn-tiny.reject:hover { background: var(--oxblood); color: var(--ink); }
  .btn-tiny.confirm { border-color: var(--gold); color: var(--gold-bright); }
  .reject-inline { display: flex; gap: 6px; align-items: center; }
  .reject-inline input { flex: 1; background: var(--ink-2); border: 1px solid var(--hairline); color: var(--parchment); padding: 5px 8px; border-radius: 4px; font-size: 12.5px; }
  .conflict-list { display: flex; flex-direction: column; gap: 14px; }
  .conflict-card { background: var(--surface-2); border: 1px solid var(--hairline); border-radius: 6px; padding: 14px 16px; }
  .conflict-head { display: flex; justify-content: space-between; font-family: var(--mono); font-size: 12px; margin-bottom: 6px; }
  .conflict-topic { font-family: var(--serif); font-size: 15px; margin-bottom: 10px; color: var(--parchment); }
  .conflict-sources { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .source-side { background: var(--ink-2); border-radius: 5px; padding: 10px; }
  .source-side .mono { font-size: 11px; color: var(--parchment-faint); display: block; margin-bottom: 4px; }
  .source-value { display: block; font-weight: 600; color: var(--gold-bright); margin-bottom: 4px; }
  .source-side blockquote { margin: 0; font-size: 12.5px; font-style: italic; color: var(--parchment-dim); border-left: 2px solid var(--hairline); padding-left: 8px; }
  .resolution { margin-top: 10px; font-size: 13px; color: var(--sage); border-top: 1px solid var(--hairline); padding-top: 8px; }
  .conflict-actions, .conflict-note { margin-top: 12px; }
  .muted { color: var(--parchment-faint); font-style: italic; }
  .file-actions { display: flex; gap: 6px; margin-top: 6px; }
  .file-actions .open-link { text-decoration: none; display: inline-block; }
  .content-warning { margin-top: 6px; font-size: 12px; color: var(--gold-bright); background: rgba(219,165,69,0.1); border: 1px solid var(--gold); border-radius: 4px; padding: 5px 8px; max-width: 420px; white-space: normal; font-family: var(--sans); }
  .source-viewer { margin-top: 16px; border: 1px solid var(--hairline); border-radius: 6px; overflow: hidden; }
  .source-viewer-head { font-size: 11px; color: var(--parchment-faint); padding: 8px 12px; border-bottom: 1px solid var(--hairline); background: var(--surface-2); }
  .source-viewer object { display: block; width: 100%; height: 640px; background: var(--ink-2); }
`;
