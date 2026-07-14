import CaseSelector from "./CaseSelector";
import NewCaseRunner from "./NewCaseRunner";
import { PHASE_1, PHASE_2, TRIGGERED } from "../pipelineDefinition";
import { deriveStageStatus, STATUS_META } from "../statusLogic";

function NavRow({ stageDef, index, active, onSelect, runState, ledgers, ocrReview }) {
  const derived = deriveStageStatus(stageDef, runState, ledgers, ocrReview);
  const meta = STATUS_META[derived.status] || STATUS_META.pending;
  return (
    <button className={`nav-row${active ? " active" : ""}`} onClick={() => onSelect(stageDef.key)}>
      <span className="nav-dot" style={{ background: meta.color }} />
      <span className="nav-index mono">{index == null ? "·" : String(index + 1).padStart(2, "0")}</span>
      <span className="nav-label">{stageDef.label}</span>
      {(derived.status === "paused" || derived.status === "halted" || derived.status === "failed") && (
        <span className="nav-flag">!</span>
      )}
    </button>
  );
}

export default function Sidebar({
  cases,
  current,
  onSelectCase,
  selectedStage,
  onSelectStage,
  runState,
  ledgers,
  ocrReview,
  onRefresh,
  onCaseListChanged,
}) {
  return (
    <nav className="sidebar">
      <div className="sidebar-top">
        <span className="eyebrow">Loss-Adjustment Pipeline</span>
        <h1>Case Docket</h1>
        <CaseSelector cases={cases} current={current} onSelect={onSelectCase} />
        <button className="refresh-btn" onClick={onRefresh}>
          ↻ refresh
        </button>
      </div>

      <NewCaseRunner onOpenCase={onSelectCase} onCaseListChanged={onCaseListChanged} />

      <div className="nav-scroll">
        <div className="nav-group-label">Phase 1 · Initial Review</div>
        {PHASE_1.map((s, i) => (
          <NavRow
            key={s.key}
            stageDef={s}
            index={i}
            active={selectedStage === s.key}
            onSelect={onSelectStage}
            runState={runState}
            ledgers={ledgers}
            ocrReview={ocrReview}
          />
        ))}
        <div className="nav-group-label">Phase 2 · Insurer Response</div>
        {PHASE_2.map((s, i) => (
          <NavRow
            key={s.key}
            stageDef={s}
            index={i}
            active={selectedStage === s.key}
            onSelect={onSelectStage}
            runState={runState}
            ledgers={ledgers}
            ocrReview={ocrReview}
          />
        ))}
        <div className="nav-group-label">Triggered · Dependency-Gated</div>
        {TRIGGERED.map((s) => (
          <NavRow
            key={s.key}
            stageDef={s}
            index={null}
            active={selectedStage === s.key}
            onSelect={onSelectStage}
            runState={runState}
            ledgers={ledgers}
            ocrReview={ocrReview}
          />
        ))}
      </div>

      <style>{`
        .sidebar {
          width: 280px; flex-shrink: 0; height: 100vh; position: sticky; top: 0;
          background: var(--ink-2); border-right: 1px solid var(--hairline);
          display: flex; flex-direction: column;
        }
        .sidebar-top { padding: 24px 20px 16px; border-bottom: 1px solid var(--hairline); }
        .eyebrow { display: block; font-family: var(--mono); font-size: 10px; letter-spacing: 0.16em; text-transform: uppercase; color: var(--gold); margin-bottom: 6px; }
        .sidebar-top h1 { font-size: 24px; margin-bottom: 16px; }
        .refresh-btn {
          margin-top: 10px; width: 100%; background: none; border: 1px solid var(--hairline); color: var(--parchment-dim);
          font-family: var(--mono); font-size: 11px; padding: 6px 10px; border-radius: 5px; cursor: pointer;
        }
        .refresh-btn:hover { border-color: var(--gold); color: var(--gold-bright); }
        .nav-scroll { flex: 1; overflow-y: auto; padding: 10px 0 24px; }
        .nav-group-label {
          font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.1em; color: var(--parchment-faint);
          padding: 14px 20px 6px;
        }
        .nav-row {
          width: 100%; display: flex; align-items: center; gap: 10px;
          padding: 8px 20px; background: none; border: none; text-align: left; cursor: pointer;
          color: var(--parchment-dim); font-size: 13px; border-left: 2px solid transparent;
        }
        .nav-row:hover { background: rgba(255,255,255,0.02); color: var(--parchment); }
        .nav-row.active { background: var(--surface); color: var(--parchment); border-left-color: var(--gold); }
        .nav-dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
        .nav-index { font-size: 11px; color: var(--parchment-faint); width: 18px; flex-shrink: 0; }
        .nav-label { flex: 1; }
        .nav-flag { color: var(--gold-bright); font-weight: 700; }
      `}</style>
    </nav>
  );
}
