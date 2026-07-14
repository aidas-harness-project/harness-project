import { useEffect, useRef, useState } from "react";
import { api } from "../api";

const POLL_MS = 5000;
const STALL_AFTER_SECONDS = 120;

function elapsedLabel(startedAt, endedAt) {
  const seconds = Math.max(0, Math.round((endedAt ? endedAt * 1000 : Date.now()) - startedAt * 1000) / 1000);
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return m ? `${m}m ${s}s` : `${s}s`;
}

// Live status strip for the case's background pipeline run. Polls while the
// run is alive so the user always sees that something is happening (or that
// it stopped); surfaces crashes and silence explicitly instead of letting a
// dead or wedged run masquerade as "running".
export default function RunBanner({ caseId, onActivity }) {
  const [status, setStatus] = useState(null);
  const [dismissed, setDismissed] = useState(false);
  const onActivityRef = useRef(onActivity);
  onActivityRef.current = onActivity;

  useEffect(() => {
    let cancelled = false;
    let timer = null;
    setDismissed(false);
    setStatus(null);

    async function tick(prev) {
      const s = await api.runStatus(caseId).catch(() => null);
      if (cancelled) return;
      setStatus(s);
      if (s?.status === "running") {
        onActivityRef.current?.(); // keep run-state/ledger views moving with the run
        timer = setTimeout(() => tick(s), POLL_MS);
      } else if (prev?.status === "running") {
        onActivityRef.current?.(); // one final refresh when the run ends
      }
    }
    tick(null);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [caseId]);

  if (!status || status.status === "not_started" || status.status === "unavailable" || dismissed) return null;

  const { status: st } = status;
  const stalled = st === "running" && (status.log_age_seconds ?? 0) > STALL_AFTER_SECONDS;
  const tone = st === "crashed" ? "bad" : st === "finished" ? "good" : stalled || st === "ended_unknown" ? "warn" : "live";

  return (
    <div className={`run-banner ${tone}`}>
      <div className="run-banner-head">
        {st === "running" && <span className="run-spinner" aria-label="run in progress" />}
        <strong>
          {st === "running" && !stalled && `Pipeline run in progress — ${elapsedLabel(status.started_at)} elapsed (pid ${status.pid})`}
          {st === "running" && stalled && `Run may be stuck — alive (pid ${status.pid}) but no log output for ${Math.round(status.log_age_seconds)}s`}
          {st === "finished" && `Run finished cleanly after ${elapsedLabel(status.started_at, status.ended_at)}`}
          {st === "crashed" && `Run CRASHED (exit code ${status.exit_code}) after ${elapsedLabel(status.started_at, status.ended_at)}`}
          {st === "ended_unknown" && "Run ended while the viewer backend was restarted — exit status unknown; check the log and run-state"}
        </strong>
        {st !== "running" && (
          <button className="run-dismiss" onClick={() => setDismissed(true)} title="dismiss">
            ×
          </button>
        )}
      </div>
      {st === "running" && !stalled && (
        <p className="run-note">Polling every {POLL_MS / 1000}s — stage statuses in the sidebar update live. A run halting at a guardrail is normal; the halt will appear on the stage it stopped at.</p>
      )}
      {(status.log_tail || "").trim() && (
        <details className="run-log" open={st === "crashed" || stalled}>
          <summary>run log (last {Math.min(status.log_tail.length, 4000)} chars{status.log_size != null ? ` of ${status.log_size}` : ""})</summary>
          <pre>{status.log_tail}</pre>
        </details>
      )}

      <style>{`
        .run-banner { margin-bottom: 22px; padding: 12px 16px; border-radius: 6px; border: 1px solid var(--hairline); background: var(--surface); font-size: 13.5px; }
        .run-banner.live { border-color: var(--slate); }
        .run-banner.good { border-color: var(--sage); }
        .run-banner.warn { border-color: var(--gold); background: rgba(219,165,69,0.08); }
        .run-banner.bad { border-color: var(--oxblood); background: rgba(180,86,70,0.1); }
        .run-banner.bad strong { color: var(--oxblood); }
        .run-banner.warn strong { color: var(--gold-bright); }
        .run-banner.good strong { color: var(--sage); }
        .run-banner-head { display: flex; align-items: center; gap: 10px; }
        .run-banner-head strong { flex: 1; font-weight: 600; }
        .run-spinner { width: 12px; height: 12px; flex-shrink: 0; border-radius: 50%; border: 2px solid var(--slate); border-top-color: transparent; animation: runspin 0.9s linear infinite; }
        @keyframes runspin { to { transform: rotate(360deg); } }
        .run-dismiss { background: none; border: none; color: var(--parchment-faint); font-size: 16px; cursor: pointer; padding: 0 4px; }
        .run-note { margin: 8px 0 0; color: var(--parchment-dim); font-size: 12.5px; }
        .run-log { margin-top: 10px; }
        .run-log summary { cursor: pointer; font-family: var(--mono); font-size: 11.5px; color: var(--parchment-faint); }
        .run-log pre { margin: 8px 0 0; max-height: 260px; overflow: auto; background: var(--ink-2); border: 1px solid var(--hairline); border-radius: 5px; padding: 10px 12px; font-size: 11.5px; line-height: 1.5; white-space: pre-wrap; word-break: break-word; }
      `}</style>
    </div>
  );
}
