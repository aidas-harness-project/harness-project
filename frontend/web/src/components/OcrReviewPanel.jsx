import { useState } from "react";
import { api } from "../api";
import { useReviewerName } from "./LedgerPanel";

// P8 resolution gate, in the UI: each blocked page's two independent
// readings side by side, so the human can compare them (and the raw
// document via the intake panel) and pick the correct one without leaving
// the screen. Resolving the last page of a document continues into
// classification -- one real model call -- so the busy state is explicit.
export default function OcrReviewPanel({ caseId, review, onResolved }) {
  const [reviewer, setReviewer] = useReviewerName();
  const [picking, setPicking] = useState(null); // {docId, page, reading}
  const [noteText, setNoteText] = useState("");
  const [busy, setBusy] = useState(null); // "docId:page" while a resolve call runs
  const [error, setError] = useState(null);

  const documents = review?.documents || [];
  if (!documents.length) {
    return <p className="muted">No unresolved OCR disagreements for this case.</p>;
  }

  async function confirmPick() {
    const { docId, page, reading } = picking;
    if (!reviewer.trim()) return setError("enter a reviewer name first");
    if (!noteText.trim()) return setError("a resolution note is required -- what did you verify, and how?");
    setBusy(`${docId}:${page}`);
    setError(null);
    try {
      await api.ocrResolve(caseId, docId, page, reading, reviewer, noteText);
      setPicking(null);
      setNoteText("");
      onResolved?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="ocr-review">
      <label className="reviewer-field">
        Reviewer name
        <input value={reviewer} onChange={(e) => setReviewer(e.target.value)} placeholder="e.g. 김태윤" />
      </label>
      {error && <p className="audit-error">{error}</p>}

      {documents.map((doc) => (
        <div className="ocr-doc" key={doc.doc_id}>
          <div className="ocr-doc-head">
            <span className="mono">{doc.doc_id}</span>
            <span className="ocr-doc-reason">{doc.review_reason}</span>
          </div>
          {!doc.raw_available && (
            <p className="audit-error">
              Raw dual-read data is missing for this document -- the disagreement predates the scratch save.
              Re-run checkpoint 1 from a terminal to regenerate both readings before resolving.
            </p>
          )}
          {doc.pages.map((p) => {
            const key = `${doc.doc_id}:${p.page}`;
            const isPicking = picking && picking.docId === doc.doc_id && picking.page === p.page;
            const isBusy = busy === key;
            return (
              <div className="ocr-page" key={p.page}>
                <div className="ocr-page-head mono">page {p.page}</div>
                {p.disagreement_details?.length > 0 && (
                  <ul className="ocr-details">
                    {p.disagreement_details.map((d, i) => (
                      <li key={i}>{d}</li>
                    ))}
                  </ul>
                )}
                <div className="ocr-readings">
                  {["reading_a", "reading_b"].map((side) => (
                    <div className={`ocr-reading${isPicking && picking.reading === side ? " chosen" : ""}`} key={side}>
                      <div className="ocr-reading-head">
                        <span className="mono">{side === "reading_a" ? "Reading A (path 1)" : "Reading B (path 2)"}</span>
                        {doc.raw_available && p[side] != null && (
                          <button
                            className="btn-tiny approve"
                            disabled={isBusy}
                            onClick={() => {
                              setPicking({ docId: doc.doc_id, page: p.page, reading: side });
                              setError(null);
                            }}
                          >
                            use this reading
                          </button>
                        )}
                      </div>
                      {p[side] != null ? (
                        <pre className="ocr-text">{p[side]}</pre>
                      ) : (
                        <p className="muted">text not available</p>
                      )}
                    </div>
                  ))}
                </div>
                {isPicking && (
                  <div className="reject-inline ocr-confirm">
                    <input
                      placeholder={`why is ${picking.reading === "reading_a" ? "A" : "B"} the correct transcription? (e.g. checked against the raw page)`}
                      value={noteText}
                      onChange={(e) => setNoteText(e.target.value)}
                      autoFocus
                    />
                    <button className="btn-tiny confirm" onClick={confirmPick} disabled={isBusy}>
                      {isBusy ? "resolving…" : `confirm ${picking.reading === "reading_a" ? "A" : "B"}`}
                    </button>
                    <button className="btn-tiny" onClick={() => setPicking(null)} disabled={isBusy}>
                      cancel
                    </button>
                  </div>
                )}
                {isBusy && (
                  <p className="ocr-busy">
                    Writing the chosen reading… if this was the last unresolved page, classification runs now
                    (one real model call) -- this can take a minute.
                  </p>
                )}
              </div>
            );
          })}
        </div>
      ))}

      <style>{`
        .ocr-review .reviewer-field { display: flex; flex-direction: column; gap: 5px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--parchment-faint); margin-bottom: 14px; max-width: 240px; }
        .ocr-review .reviewer-field input { background: var(--surface); border: 1px solid var(--hairline); color: var(--parchment); padding: 6px 10px; border-radius: 4px; font-family: var(--sans); font-size: 13px; text-transform: none; }
        .ocr-review .audit-error { color: var(--oxblood); font-size: 12.5px; margin-bottom: 10px; }
        .ocr-doc { border: 1px solid var(--hairline); border-radius: 6px; padding: 14px 16px; margin-bottom: 16px; background: var(--surface-2); }
        .ocr-doc-head { display: flex; gap: 12px; align-items: baseline; margin-bottom: 10px; }
        .ocr-doc-head .mono { color: var(--gold-bright); font-size: 12px; }
        .ocr-doc-reason { font-size: 12.5px; color: var(--parchment-dim); }
        .ocr-page { border-top: 1px solid var(--hairline); padding-top: 12px; margin-top: 12px; }
        .ocr-page-head { font-size: 11.5px; color: var(--parchment-faint); margin-bottom: 8px; }
        .ocr-details { margin: 0 0 10px; padding-left: 18px; font-size: 12px; color: var(--gold-bright); }
        .ocr-readings { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
        .ocr-reading { background: var(--ink-2); border: 1px solid var(--hairline); border-radius: 5px; padding: 10px; }
        .ocr-reading.chosen { border-color: var(--sage); }
        .ocr-reading-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-bottom: 8px; }
        .ocr-reading-head .mono { font-size: 11px; color: var(--parchment-faint); }
        .ocr-text { margin: 0; max-height: 300px; overflow: auto; font-size: 12px; line-height: 1.55; white-space: pre-wrap; word-break: break-word; }
        .ocr-confirm { margin-top: 10px; }
        .ocr-busy { margin: 8px 0 0; font-size: 12.5px; color: var(--gold-bright); }
        .reject-inline { display: flex; gap: 6px; align-items: center; }
        .reject-inline input { flex: 1; background: var(--ink-2); border: 1px solid var(--hairline); color: var(--parchment); padding: 5px 8px; border-radius: 4px; font-size: 12.5px; }
        .btn-tiny { font-family: var(--mono); font-size: 11px; padding: 5px 10px; border-radius: 4px; cursor: pointer; background: var(--surface-2); border: 1px solid var(--hairline); color: var(--parchment-dim); }
        .btn-tiny:disabled { opacity: 0.5; cursor: wait; }
        .btn-tiny.approve { border-color: var(--sage); color: var(--sage); }
        .btn-tiny.approve:hover { background: var(--sage); color: var(--ink); }
        .btn-tiny.confirm { border-color: var(--gold); color: var(--gold-bright); }
        .muted { color: var(--parchment-faint); font-style: italic; }
      `}</style>
    </div>
  );
}
