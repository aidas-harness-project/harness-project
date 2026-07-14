import { useEffect, useState } from "react";
import StatusStamp from "./StatusStamp";
import ContractView from "./ContractView";
import ReportViewer from "./ReportViewer";
import { SourceLedgerPanel, ConflictLedgerPanel } from "./LedgerPanel";
import OcrReviewPanel from "./OcrReviewPanel";
import HumanReviewPanel from "./HumanReviewPanel";
import { deriveStageStatus } from "../statusLogic";
import { api } from "../api";

function Checkpoint({ cp, active }) {
  return (
    <div className={`checkpoint${active ? " active" : ""}`}>
      <span className="checkpoint-dot" />
      {cp.label}
    </div>
  );
}

export default function StageDetail({ stageDef, index, phaseLabel, runState, ledgers, ocrReview, caseId, onLedgersChanged }) {
  const [contracts, setContracts] = useState({});
  const [report, setReport] = useState(null);
  const derived = deriveStageStatus(stageDef, runState, ledgers, ocrReview);

  useEffect(() => {
    let cancelled = false;
    setContracts({});
    setReport(null);

    if (stageDef.contracts) {
      stageDef.contracts.forEach((name) => {
        api
          .contract(caseId, name)
          .then((data) => !cancelled && setContracts((c) => ({ ...c, [name]: data })))
          .catch(() => !cancelled && setContracts((c) => ({ ...c, [name]: undefined })));
      });
    }
    if (stageDef.report) {
      api
        .report(caseId, stageDef.report)
        .then((data) => !cancelled && setReport(data))
        .catch(() => !cancelled && setReport({ missing: true }));
    }
    return () => {
      cancelled = true;
    };
  }, [stageDef.key, caseId]);

  return (
    <div className="stage-detail">
      <div className="detail-header">
        <div className="detail-heading">
          <span className="detail-eyebrow mono">
            {phaseLabel} · Step {String(index + 1).padStart(2, "0")}
          </span>
          <h2>{stageDef.label}</h2>
          {stageDef.agent && <span className="detail-agent mono">agent: {stageDef.agent}</span>}
        </div>
        <StatusStamp status={derived.status} />
      </div>

      {derived.reason && (
        <div className="paused-banner">
          <strong>{derived.rule ? `[${derived.rule}] ` : ""}Paused</strong> — {derived.reason}
        </div>
      )}

      <p className="stage-desc">{stageDef.description}</p>

      {stageDef.checkpoints.length > 0 && (
        <div className="checkpoint-list">
          {stageDef.checkpoints.map((cp) => (
            <Checkpoint key={cp.key} cp={cp} active={derived.status === "passed"} />
          ))}
        </div>
      )}

      {stageDef.key === "case-intake" && (
        <div className="detail-block">
          <h5 className="mono">_source_ledger.json</h5>
          <SourceLedgerPanel ledger={ledgers?.source_ledger} caseId={caseId} onChanged={onLedgersChanged} />
        </div>
      )}
      {stageDef.key === "document-pipeline" && (
        <div className="detail-block">
          <h5 className="mono">P8 dual-read review</h5>
          <OcrReviewPanel caseId={caseId} review={ocrReview} onResolved={onLedgersChanged} />
        </div>
      )}
      {stageDef.key === "consistency-check" && (
        <div className="detail-block">
          <h5 className="mono">_conflict_ledger.json</h5>
          <ConflictLedgerPanel ledger={ledgers?.conflict_ledger} caseId={caseId} onChanged={onLedgersChanged} />
        </div>
      )}
      {stageDef.reviewVersion && (
        <div className="detail-block">
          <h5 className="mono">human review gate ({stageDef.reviewVersion})</h5>
          <HumanReviewPanel caseId={caseId} version={stageDef.reviewVersion} onChanged={onLedgersChanged} />
        </div>
      )}

      {stageDef.contracts?.map((name) => (
        <div className="detail-block" key={name}>
          <h5 className="mono">{name}</h5>
          {contracts[name] === undefined ? (
            <p className="muted">not yet available</p>
          ) : contracts[name] ? (
            <ContractView data={contracts[name]} />
          ) : (
            <p className="muted">loading…</p>
          )}
        </div>
      ))}

      {stageDef.report && (
        <div className="detail-block">
          <h5 className="mono">{stageDef.report}</h5>
          {report?.missing ? (
            <p className="muted">not yet available</p>
          ) : report ? (
            <ReportViewer markdown={report.markdown} evidence={report.evidence} />
          ) : (
            <p className="muted">loading…</p>
          )}
        </div>
      )}

      <style>{`
        .stage-detail { max-width: 760px; animation: riseIn 0.3s ease both; }
        .detail-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 18px; gap: 16px; }
        .detail-eyebrow { display: block; font-size: 11px; letter-spacing: 0.1em; text-transform: uppercase; color: var(--parchment-faint); margin-bottom: 6px; }
        .detail-heading h2 { font-size: 30px; margin-bottom: 4px; }
        .detail-agent { font-size: 12px; color: var(--parchment-faint); }
        .paused-banner {
          margin-bottom: 18px; padding: 12px 16px; background: rgba(219,165,69,0.1);
          border: 1px solid var(--gold); border-radius: 6px; font-size: 13.5px; color: var(--gold-bright);
        }
        .stage-desc { color: var(--parchment-dim); font-size: 14.5px; margin-bottom: 20px; line-height: 1.6; }
        .checkpoint-list { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 24px; }
        .checkpoint { display: flex; align-items: center; gap: 6px; font-size: 12.5px; color: var(--parchment-faint); background: var(--surface); border: 1px solid var(--hairline); padding: 6px 12px; border-radius: 5px; }
        .checkpoint-dot { width: 5px; height: 5px; border-radius: 50%; background: var(--hairline); }
        .checkpoint.active .checkpoint-dot { background: var(--sage); }
        .checkpoint.active { color: var(--parchment); border-color: var(--sage); }
        .detail-block { margin-top: 26px; padding-top: 22px; border-top: 1px solid var(--hairline); }
        .detail-block h5 { font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--parchment-faint); margin: 0 0 12px; font-weight: 500; }
        .muted { color: var(--parchment-faint); font-style: italic; }
      `}</style>
    </div>
  );
}
