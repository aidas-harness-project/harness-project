import { useEffect, useState } from "react";
import { api } from "../api";
import { useReviewerName } from "./LedgerPanel";

function Step({ done, children }) {
  return (
    <li className={done ? "done" : "todo"}>
      <span className="step-mark mono">{done ? "✓" : "·"}</span> {children}
    </li>
  );
}

// The critic -> human review -> evaluation gate (P7/D1), driven from the UI.
// Shows exactly what the pipeline is waiting on for this draft version and
// lets the human open the D1 gate once the recorded review content exists.
// dao.py enforces the hard precondition (expert_review_v{N}.json must exist
// and validate) -- the button can't self-certify past it.
export default function HumanReviewPanel({ caseId, version, onChanged }) {
  const [reviewer, setReviewer] = useReviewerName();
  const [state, setState] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setState(null);
    api
      .humanReview(caseId)
      .then((data) => !cancelled && setState(data?.[version] || null))
      .catch((e) => !cancelled && setError(e.message));
    return () => {
      cancelled = true;
    };
  }, [caseId, version]);

  if (!state) return error ? <p className="audit-error">{error}</p> : <p className="muted">loading…</p>;

  async function markComplete() {
    if (!reviewer.trim()) return setError("enter a reviewer name first");
    setBusy(true);
    setError(null);
    try {
      await api.markHumanReviewComplete(caseId, version, reviewer);
      const data = await api.humanReview(caseId);
      setState(data?.[version] || null);
      onChanged?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="human-review">
      <ol className="review-steps">
        <Step done={state.reviewed_draft_exists}>
          <span className="mono">draft_report_{version}_reviewed.md</span> exists (critic's annotated draft)
        </Step>
        <Step done={state.expert_review_exists}>
          <span className="mono">expert_review_{version}.json</span> exists (the recorded human review content)
        </Step>
        <Step done={state.review_complete}>
          D1 gate open — evaluation may read ground truth for {version}
          {state.review_complete && (
            <span className="muted"> (marked by {state.completed_by} at {state.completed_at})</span>
          )}
        </Step>
      </ol>

      {error && <p className="audit-error">{error}</p>}

      {!state.review_complete && (
        <div className="gate-action">
          {state.expert_review_exists ? (
            <>
              <label className="reviewer-field">
                Reviewer name
                <input value={reviewer} onChange={(e) => setReviewer(e.target.value)} placeholder="e.g. 김태윤" />
              </label>
              <button className="btn-tiny confirm" onClick={markComplete} disabled={busy}>
                {busy ? "marking…" : `mark human review complete (${version})`}
              </button>
              <p className="muted small">
                This is a real human action — it creates the versioned D1 flag that lets evaluation read the
                answer key. Only do this after actually reviewing the draft.
              </p>
            </>
          ) : (
            <p className="muted">
              The gate can't open yet: the recorded review content ({" "}
              <span className="mono">expert_review_{version}.json</span> ) doesn't exist. The pipeline (or a
              human transcribing their review) writes it via the DAO first — you cannot certify a review that
              was never recorded.
            </p>
          )}
        </div>
      )}

      <style>{`
        .review-steps { list-style: none; margin: 0 0 14px; padding: 0; font-size: 13.5px; }
        .review-steps li { padding: 5px 0; color: var(--parchment-dim); }
        .review-steps li.done { color: var(--parchment); }
        .step-mark { display: inline-block; width: 18px; color: var(--sage); }
        .review-steps li.todo .step-mark { color: var(--parchment-faint); }
        .human-review .reviewer-field { display: flex; flex-direction: column; gap: 5px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--parchment-faint); margin-bottom: 10px; max-width: 240px; }
        .human-review .reviewer-field input { background: var(--surface); border: 1px solid var(--hairline); color: var(--parchment); padding: 6px 10px; border-radius: 4px; font-family: var(--sans); font-size: 13px; text-transform: none; }
        .human-review .audit-error { color: var(--oxblood); font-size: 12.5px; margin: 8px 0; }
        .btn-tiny { font-family: var(--mono); font-size: 11px; padding: 6px 12px; border-radius: 4px; cursor: pointer; background: var(--surface-2); border: 1px solid var(--gold); color: var(--gold-bright); }
        .btn-tiny:disabled { opacity: 0.5; cursor: wait; }
        .muted { color: var(--parchment-faint); font-style: italic; }
        .muted.small { font-size: 12px; margin-top: 8px; }
        .gate-action { border-top: 1px solid var(--hairline); padding-top: 12px; }
      `}</style>
    </div>
  );
}
