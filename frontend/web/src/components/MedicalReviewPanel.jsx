import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { clearDurableOperation, durableOperationId } from "../durableOperationId";
import { buildMedicalWorkspace, currentRequestRevision, evidenceMatchesCoordinate, medicalOperationSignature, planActionCompletion } from "../medicalReviewUiLogic";
import MedicalVariablesPanel from "./MedicalVariablesPanel";
import MedicalReviewPackage, { StructuredMedicalRecord } from "./MedicalReviewPackage";
import MedicalReviewResponseForm from "./MedicalReviewResponseForm";

export default function MedicalReviewPanel({ caseId }) {
  const [unlocked, setUnlocked] = useState(() => api.hasMedicalToken());
  const [token, setToken] = useState("");
  const [variables, setVariables] = useState(null);
  const [ledger, setLedger] = useState(null);
  const [runState, setRunState] = useState(null);
  const [evidence, setEvidence] = useState(null);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [selectedReviewItemId, setSelectedReviewItemId] = useState(null);
  const [reconcileGeneration, setReconcileGeneration] = useState(0);
  const loadGeneration = useRef(0);
  const evidenceGeneration = useRef(0);
  const actionGeneration = useRef(0);
  const reconciledGeneration = useRef(0);

  const load = useCallback(async ({ preferredReviewItemId = null } = {}) => {
    if (!unlocked || !caseId) return;
    const generation = ++loadGeneration.current;
    evidenceGeneration.current += 1;
    setLoading(true);
    setError(null);
    setVariables(null);
    setLedger(null);
    setRunState(null);
    setEvidence(null);
    if (!preferredReviewItemId) setSelectedReviewItemId(null);
    try {
      const [nextLedger, nextRunState] = await Promise.all([
        api.medicalReviews(caseId),
        api.runState(caseId),
      ]);
      if (generation !== loadGeneration.current) return;
      const nextItem = nextLedger.review_items?.find(
        (entry) => entry.review_item_id === preferredReviewItemId,
      ) || nextLedger.review_items?.[0] || null;
      const nextItemId = nextItem?.review_item_id || null;
      const revisionSha = currentRequestRevision(nextLedger, nextItemId);
      const nextVariables = await api.medicalVariables(caseId, revisionSha);
      if (generation !== loadGeneration.current) return;
      setVariables(nextVariables);
      setLedger(nextLedger);
      setRunState(nextRunState);
      setSelectedReviewItemId(nextItemId);
    } catch (nextError) {
      if (generation === loadGeneration.current) setError(nextError.message);
    } finally {
      if (generation === loadGeneration.current) setLoading(false);
    }
  }, [caseId, unlocked]);

  useEffect(() => {
    setBusy(false);
    setNotice(null);
    load();
    return () => {
      loadGeneration.current += 1;
      evidenceGeneration.current += 1;
      actionGeneration.current += 1;
    };
  }, [load]);
  useEffect(() => {
    if (!unlocked || reconcileGeneration === reconciledGeneration.current) return;
    reconciledGeneration.current = reconcileGeneration;
    load({ preferredReviewItemId: selectedReviewItemId });
  }, [load, reconcileGeneration, selectedReviewItemId, unlocked]);
  const workspace = useMemo(
    () => buildMedicalWorkspace(
      variables, ledger, selectedReviewItemId, runState,
    ),
    [variables, ledger, runState, selectedReviewItemId],
  );

  const unlock = (event) => {
    event.preventDefault();
    api.setMedicalToken(token);
    setUnlocked(Boolean(token.trim()));
    setToken("");
  };
  const lock = () => {
    loadGeneration.current += 1;
    evidenceGeneration.current += 1;
    actionGeneration.current += 1;
    api.setMedicalToken("");
    setUnlocked(false);
    setVariables(null);
    setLedger(null);
    setRunState(null);
    setEvidence(null);
    setBusy(false);
    setLoading(false);
    setNotice(null);
  };
  const selectReviewItem = async (reviewItemId) => {
    if (!ledger || reviewItemId === selectedReviewItemId) return;
    const generation = ++loadGeneration.current;
    evidenceGeneration.current += 1;
    actionGeneration.current += 1;
    setBusy(false);
    setNotice(null);
    setSelectedReviewItemId(reviewItemId);
    setLoading(true);
    setError(null);
    setVariables(null);
    setEvidence(null);
    try {
      const revisionSha = currentRequestRevision(ledger, reviewItemId);
      const nextVariables = await api.medicalVariables(caseId, revisionSha);
      if (generation !== loadGeneration.current) return;
      setVariables(nextVariables);
    } catch (nextError) {
      if (generation === loadGeneration.current) setError(nextError.message);
    } finally {
      if (generation === loadGeneration.current) setLoading(false);
    }
  };
  const openEvidence = async (locatorId) => {
    if (!workspace.item || !workspace.request) return;
    const coordinate = {
      caseId,
      reviewItemId: workspace.item.review_item_id,
      requestId: workspace.request.request_id,
      requestVersion: workspace.request.request_version,
      revisionSha: workspace.request.medical_variables_revision?.sha256,
      locatorId,
    };
    const generation = ++evidenceGeneration.current;
    setError(null);
    setEvidence(null);
    try {
      const nextEvidence = await api.medicalEvidence(
        caseId,
        coordinate.reviewItemId,
        coordinate.requestId,
        coordinate.requestVersion,
        locatorId,
      );
      if (generation !== evidenceGeneration.current) return;
      if (!evidenceMatchesCoordinate(nextEvidence, coordinate)) {
        throw new Error("Evidence response did not match the selected request coordinate.");
      }
      setEvidence(nextEvidence);
    } catch (nextError) {
      if (generation === evidenceGeneration.current) setError(nextError.message);
    }
  };
  const applyAction = async (action, body) => {
    if (!workspace.item) return;
    const generation = ++actionGeneration.current;
    const itemId = workspace.item.review_item_id;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const pendingOperation = await durableOperationId(
        `medical:${caseId}:${itemId}`,
        medicalOperationSignature(action, body, workspace.events),
        "medical",
      );
      const result = await api.medicalReviewAction(
        caseId, itemId, action, body, pendingOperation.operationId,
      );
      clearDurableOperation(pendingOperation.key);
      const completion = planActionCompletion(
        generation, actionGeneration.current, result.status,
      );
      if (completion.projectionPending) {
        setNotice("The lifecycle event committed, but the run-state projection needs recovery.");
      }
      if (generation !== actionGeneration.current) {
        if (completion.reconcile) setReconcileGeneration((value) => value + 1);
        return;
      }
      setEvidence(null);
      await load({ preferredReviewItemId: itemId });
    } catch (nextError) {
      const completion = planActionCompletion(
        generation, actionGeneration.current, "failed",
      );
      if (completion.isCurrent) setError(nextError.message);
    } finally {
      const completion = planActionCompletion(
        generation, actionGeneration.current, "failed",
      );
      if (completion.releaseBusy) setBusy(false);
    }
  };

  if (!api.medicalWorkspaceAvailable) {
    return (
      <section className="medical-auth-card">
        <h3>Medical review workspace</h3>
        <p>Medical workspace is unavailable in this static snapshot. Connect the localhost backend and an approved operator policy to use medical records or actions.</p>
        <style>{`.medical-auth-card { margin-top:28px; border:1px solid var(--hairline); border-radius:10px; background:rgba(38,32,25,.72); padding:22px; }.medical-auth-card h3 { font-size:24px; }.medical-auth-card p { color:var(--parchment-dim); margin-top:7px; }`}</style>
      </section>
    );
  }

  if (!unlocked) {
    return (
      <section className="medical-auth-card">
        <h3>Medical review workspace</h3>
        <p>Authenticate a locally approved medical operator to read or mutate the canonical medical review.</p>
        <form onSubmit={unlock}>
          <label><span>Operator token</span><input type="password" autoComplete="off" value={token} onChange={(event) => setToken(event.target.value)} /></label>
          <button type="submit" disabled={!token.trim()}>Unlock workspace</button>
        </form>
        <style>{`
          .medical-auth-card { margin-top:28px; border:1px solid var(--hairline); border-radius:10px; background:rgba(38,32,25,.72); padding:22px; }
          .medical-auth-card h3 { font-size:24px; }
          .medical-auth-card p { color:var(--parchment-dim); margin:7px 0 14px; }
          .medical-auth-card form { display:grid; grid-template-columns:minmax(0,1fr) auto; align-items:end; gap:9px; }
          .medical-auth-card label span { display:block; font-size:12px; color:var(--parchment-dim); margin-bottom:4px; }
          .medical-auth-card input { width:100%; border:1px solid var(--hairline); border-radius:5px; color:var(--parchment); background:var(--ink-2); padding:9px; font:inherit; }
          .medical-auth-card button { border:1px solid var(--gold); color:var(--gold-bright); background:rgba(219,165,69,.13); border-radius:5px; padding:10px 14px; cursor:pointer; }
          .medical-auth-card button:disabled { opacity:.45; cursor:not-allowed; }
          @media (max-width:700px) { .medical-auth-card form { grid-template-columns:1fr; } }
        `}</style>
      </section>
    );
  }

  return (
    <section className="medical-workspace">
      <header className="medical-workspace-header">
        <div><span className="mono">authenticated · DAO mediated</span><h3>Medical review workspace</h3></div>
        <div className="medical-header-actions"><button type="button" onClick={() => load({ preferredReviewItemId: selectedReviewItemId })}>Refresh</button><button type="button" onClick={lock}>Lock</button></div>
      </header>
      <p className="medical-safety-banner">Request for human medical interpretation, not an adverse finding. Localhost-only authenticated workflow.</p>
      {error && <p className="medical-error">{error}</p>}
      {notice && <p className="medical-warning">{notice}</p>}
      {loading && !variables && <p className="medical-empty">Loading canonical medical records…</p>}
      {variables && ledger && <>
        {workspace.items.length > 1 && <nav className="medical-item-tabs" aria-label="Medical review issues">
          {workspace.items.map((entry) => <button type="button" key={entry.review_item_id} className={entry.review_item_id === workspace.item?.review_item_id ? "active" : ""} onClick={() => selectReviewItem(entry.review_item_id)}>
            {entry.review_item_id} · {entry.issue_id}
          </button>)}
        </nav>}
        <div className="medical-referral-grid">
          <div><span>Referral status</span><strong>{workspace.referral?.decision?.replaceAll("_", " ") || "Decision pending"}</strong></div>
          <div><span>Referral rationale</span><p>{workspace.referral?.rationale || "No referral decision has been recorded."}</p></div>
          <div><span>Lifecycle state</span><strong>{workspace.item?.state?.replaceAll("_", " ") || "No review item"}</strong></div>
        </div>
        <MedicalVariablesPanel workspace={workspace} onOpenEvidence={openEvidence} />
        <MedicalReviewPackage item={workspace.item} request={workspace.request} requestHistory={workspace.requestHistory} responseHistory={workspace.responseHistory} assignmentHistory={workspace.assignmentHistory} sourceReinspectionHistory={workspace.sourceReinspectionHistory} evidence={evidence} onOpenEvidence={openEvidence} />
        <section className="medical-section medical-waits">
          <h4>Expert input status</h4>
          {workspace.waitProjectionErrors.map((message) => (
            <p className="medical-error" key={message}>{message}</p>
          ))}
          {workspace.waits.length ? <ul>{workspace.waits.map((wait) => <li key={wait.human_input_id}>
            <strong>{wait.input_kind.replaceAll("_", " ")}</strong>
            <span>{wait.status.replaceAll("_", " ")}</span>
            <small>{wait.resolution || "No resolution recorded"}</small>
            <details><summary>Complete canonical wait episode</summary><StructuredMedicalRecord value={wait} /></details>
          </li>)}</ul> : <p className="medical-empty">No expert-input wait is recorded for this item.</p>}
        </section>
        <section className="medical-section medical-history">
          <h4>Complete event history</h4>
          {workspace.events.length ? <ol>{workspace.events.map((event) => <li key={event.event_id}>
            <span className="mono">{event.event_id}</span> <strong>{event.action?.replaceAll("_", " ")}</strong>
            <small>{event.from_state} → {event.to_state}{event.created_at ? ` · ${event.created_at}` : ""}</small>
            <small>{event.actor ? `${event.actor.display_name} · ${event.actor.declared_role} · ${event.actor.actor_id}` : "system process"}{event.reason ? ` · ${event.reason}` : ""}</small>
            <details><summary>Complete canonical lifecycle event and role policy snapshot</summary><StructuredMedicalRecord value={event} /></details>
          </li>)}</ol> : <p className="medical-empty">No lifecycle events for this review item.</p>}
        </section>
        <section className="medical-section medical-response">
          <h4>Medical response</h4>
          {workspace.response ? <><div className="medical-response-grid">
            <div><span>Response identity</span><p><strong>{workspace.response.response_id}</strong> · version {workspace.response.response_version} · {workspace.response.response_status}</p></div>
            <div><span>Reviewer attribution</span><p>{workspace.response.reviewer?.display_name} ({workspace.response.reviewer?.declared_role}, {workspace.response.reviewer?.specialty_code})</p></div>
            <div><span>Reviewed request / assignment</span><p>{workspace.response.request_id} · version {workspace.response.request_version_reviewed} · {workspace.response.assignment_id}</p></div>
            <div><span>Reviewed policy versions</span><p>{workspace.response.request_config_version_reviewed} · {workspace.response.referral_policy_version_reviewed || "No referral policy"}</p></div>
            <div><span>Reviewed evidence locators</span><p>{workspace.response.evidence_locator_ids_reviewed?.join(", ")}</p></div>
            <div><span>Supersession / attestation</span><p>{workspace.response.supersedes_response_id || "Original response"} · {workspace.response.attestation}</p></div>
            <div><span>Interpretation</span><p>{workspace.response.interpretation}</p></div>
            <div><span>Basis</span><p>{workspace.response.basis}</p></div>
            <div><span>Uncertainty</span><p>{workspace.response.uncertainty}</p></div>
            <div><span>Alternative interpretations</span><p>{workspace.response.alternative_interpretations?.join("; ") || "None recorded"}</p></div>
            <div><span>Downstream adjustment advice</span><p>{workspace.response.downstream_adjustment_advice || "None recorded"}</p></div>
            <div><span>Reviewed / submitted</span><p>{workspace.response.reviewed_at} / {workspace.response.submitted_at}</p></div>
          </div><details><summary>Complete canonical current response</summary><StructuredMedicalRecord value={workspace.response} /></details></> : <p className="medical-empty">No current medical response.</p>}
        </section>
        <MedicalReviewResponseForm item={workspace.item} request={workspace.request} response={workspace.response} busy={busy} onAction={applyAction} />
      </>}
      <style>{`
        .medical-workspace, .medical-auth-card { margin-top: 28px; border: 1px solid var(--hairline); border-radius: 10px; background: rgba(38,32,25,.72); padding: 22px; }
        .medical-workspace-header, .medical-package-heading { display:flex; align-items:flex-start; justify-content:space-between; gap:16px; }
        .medical-workspace-header span { color:var(--sage); font-size:11px; text-transform:uppercase; letter-spacing:.08em; }
        .medical-workspace-header h3, .medical-auth-card h3 { font-size:24px; }
        .medical-header-actions { display:flex; gap:8px; }
        .medical-header-actions button, .medical-auth-card button, .medical-link-button, .medical-primary { border:1px solid var(--hairline); color:var(--parchment); background:var(--surface-2); border-radius:5px; padding:7px 11px; cursor:pointer; }
        .medical-primary { border-color:var(--gold); background:rgba(219,165,69,.13); color:var(--gold-bright); margin-top:8px; }
        .medical-error { margin:14px 0; padding:10px 12px; border:1px solid var(--oxblood); color:#e6a295; border-radius:6px; }
        .medical-safety-banner { margin:14px 0; padding:10px 12px; border-left:3px solid var(--gold); background:rgba(219,165,69,.1); color:var(--gold-bright); }
        .medical-item-tabs { display:flex; flex-wrap:wrap; gap:7px; margin:14px 0; }.medical-item-tabs button { border:1px solid var(--hairline); border-radius:18px; background:var(--ink-2); color:var(--parchment-dim); padding:6px 10px; cursor:pointer; }.medical-item-tabs button.active { border-color:var(--gold); color:var(--gold-bright); }
        .medical-history ol { display:grid; gap:7px; padding-left:22px; }.medical-history li { padding:8px 10px; background:var(--ink-2); }.medical-history small { display:block; color:var(--parchment-faint); margin-top:3px; }
        .medical-referral-grid { display:grid; grid-template-columns:1fr 2fr 1fr; gap:1px; background:var(--hairline); border:1px solid var(--hairline); border-radius:7px; overflow:hidden; margin:20px 0; }
        .medical-referral-grid > div { background:var(--ink-2); padding:14px; }
        .medical-referral-grid span, .medical-response-grid span { display:block; font-size:11px; text-transform:uppercase; color:var(--parchment-faint); margin-bottom:5px; }
        .medical-section, .medical-package, .medical-action-form { margin-top:24px; padding-top:20px; border-top:1px solid var(--hairline); }
        .medical-section h4, .medical-package h4, .medical-action-form h4 { font-size:20px; margin-bottom:12px; }
        .medical-timeline { display:grid; gap:8px; }
        .medical-timeline-entry { display:grid; grid-template-columns:34px 1fr; gap:10px; border-left:2px solid var(--slate); padding:10px 12px; background:var(--ink-2); }
        .medical-timeline-index { color:var(--slate); }
        .medical-timeline-entry small, .medical-conflict small { color:var(--parchment-faint); }
        .medical-conflict { margin-top:10px; border:1px solid var(--oxblood); border-radius:6px; overflow:hidden; }
        .medical-conflict header { padding:7px 10px; color:#e6a295; background:rgba(180,86,70,.12); }
        .medical-conflict-grid, .medical-evidence-grid, .medical-response-grid, .medical-package-copy { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; }
        .medical-conflict-grid > div, .medical-evidence-card, .medical-response-grid > div, .medical-package-copy > div { padding:12px; background:var(--ink-2); }
        .medical-evidence-grid { margin-top:24px; }
        .medical-evidence-column { border-top:2px solid var(--sage); padding-top:10px; }
        .medical-evidence-column.countervailing { border-color:var(--oxblood); }
        .medical-evidence-column h5, .medical-package h5 { font-size:12px; text-transform:uppercase; letter-spacing:.06em; color:var(--parchment-dim); }
        .medical-evidence-card { margin-top:8px; border:1px solid var(--hairline); }
        .medical-evidence-card small { display:block; color:var(--parchment-dim); margin-top:5px; }
        .medical-chip-row { display:flex; flex-wrap:wrap; gap:6px; margin-top:9px; }
        .medical-link-button { font-family:var(--mono); font-size:11px; padding:4px 7px; color:var(--gold-bright); }
        .medical-package-heading .mono { color:var(--parchment-faint); font-size:11px; }
        .medical-state { border:1px solid var(--sage); color:var(--sage); border-radius:20px; padding:4px 9px; font-size:11px; text-transform:uppercase; }
        .medical-question { font-family:var(--serif); font-size:19px; margin:12px 0; }
        .medical-facts { display:grid; grid-template-columns:repeat(3,1fr); margin:0 0 12px; }
        .medical-facts dt { color:var(--parchment-faint); font-size:11px; text-transform:uppercase; }.medical-facts dd { margin:2px 0 0; }
        .medical-package-copy { margin:12px 0; }.medical-package-copy ul { margin:4px 0; padding-left:18px; }
        .medical-evidence-detail { margin-top:12px; padding:13px; border-left:3px solid var(--gold); background:var(--ink-2); }.medical-evidence-detail span,.medical-evidence-detail strong { display:block; }
        .medical-field { display:grid; gap:5px; margin:10px 0; }.medical-field span { color:var(--parchment-dim); font-size:12px; }.medical-field input,.medical-field textarea,.medical-field select,.medical-auth-card input { width:100%; border:1px solid var(--hairline); border-radius:5px; color:var(--parchment); background:var(--ink-2); padding:9px; font:inherit; }.medical-field textarea { min-height:76px; resize:vertical; }
        .medical-auth-card p { color:var(--parchment-dim); margin:7px 0 14px; }.medical-auth-card form { display:grid; grid-template-columns:1fr auto; align-items:end; gap:9px; }.medical-auth-card label span { display:block; font-size:12px; color:var(--parchment-dim); margin-bottom:4px; }
        .medical-empty { color:var(--parchment-faint); font-style:italic; }
        .medical-warning { color:#ffd78a; border-left:3px solid #dba545; padding:8px 10px; margin:10px 0; background:rgba(219,165,69,.08); }
        .medical-canonical-record, .medical-package details, .medical-waits details, .medical-history details, .medical-response details, .medical-evidence-detail details { margin-top:10px; border-top:1px solid var(--hairline); padding-top:8px; }
        .medical-package summary, .medical-waits summary, .medical-history summary, .medical-response summary, .medical-evidence-detail summary { color:var(--gold-bright); cursor:pointer; font-size:12px; }
        .medical-provenance-record { display:grid; gap:7px; margin:8px 0; }.medical-provenance-record > div { min-width:0; }.medical-provenance-record dt { color:var(--parchment-faint); font-size:10px; text-transform:uppercase; }.medical-provenance-record dd { margin:2px 0 0; overflow-wrap:anywhere; }.medical-provenance-array { margin:3px 0; padding-left:18px; }.medical-provenance-empty { color:var(--parchment-faint); font-style:italic; }
        @media (max-width:850px) { .medical-referral-grid,.medical-conflict-grid,.medical-evidence-grid,.medical-response-grid,.medical-package-copy { grid-template-columns:1fr; }.medical-facts { grid-template-columns:1fr; gap:8px; } }
      `}</style>
    </section>
  );
}
