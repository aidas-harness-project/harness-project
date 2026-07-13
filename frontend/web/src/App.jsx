import { useEffect, useState, useCallback } from "react";
import Sidebar from "./components/Sidebar";
import StageDetail from "./components/StageDetail";
import { PHASE_1, PHASE_2, ALL_STAGES } from "./pipelineDefinition";
import { api } from "./api";

function useCaseData(caseId) {
  const [runState, setRunState] = useState(null);
  const [ledgers, setLedgers] = useState(null);
  const [error, setError] = useState(null);

  const reload = useCallback(() => {
    if (!caseId) return;
    setError(null);
    Promise.all([api.runState(caseId), api.ledgers(caseId)])
      .then(([rs, lg]) => {
        setRunState(rs);
        setLedgers(lg);
      })
      .catch((e) => setError(e.message));
  }, [caseId]);

  useEffect(reload, [reload]);
  return { runState, ledgers, error, reload };
}

export default function App() {
  const [cases, setCases] = useState([]);
  const [current, setCurrent] = useState(null);
  const [selectedStage, setSelectedStage] = useState(PHASE_1[0].key);
  const { runState, ledgers, error, reload } = useCaseData(current);

  const refreshCaseList = useCallback(() => {
    api
      .listCases()
      .then((list) => {
        setCases(list);
        setCurrent((cur) => cur ?? (list.length ? list[0] : null));
      })
      .catch(() => {});
  }, []);

  useEffect(refreshCaseList, [refreshCaseList]);

  const stageIndex = ALL_STAGES.findIndex((s) => s.key === selectedStage);
  const stageDef = ALL_STAGES[stageIndex];
  const phaseLabel = PHASE_1.some((s) => s.key === selectedStage) ? "Phase 1" : "Phase 2";
  const indexWithinPhase = phaseLabel === "Phase 1" ? stageIndex : stageIndex - PHASE_1.length;

  return (
    <div className="app-shell">
      <Sidebar
        cases={cases}
        current={current}
        onSelectCase={setCurrent}
        selectedStage={selectedStage}
        onSelectStage={setSelectedStage}
        runState={runState}
        ledgers={ledgers}
        onRefresh={reload}
        onCaseListChanged={refreshCaseList}
      />

      <main className="app-main">
        {!current && <p className="muted">Select a case to begin.</p>}
        {error && <p className="error-banner">{error}</p>}
        {current && runState && stageDef && (
          <StageDetail
            stageDef={stageDef}
            index={indexWithinPhase}
            phaseLabel={phaseLabel}
            runState={runState}
            ledgers={ledgers}
            caseId={current}
            onLedgersChanged={reload}
          />
        )}
      </main>

      <style>{`
        .app-shell { display: flex; min-height: 100vh; }
        .app-main { flex: 1; padding: 40px 48px; overflow-y: auto; max-height: 100vh; }
        .muted { color: var(--parchment-faint); font-style: italic; }
        .error-banner { color: var(--oxblood); background: rgba(180,86,70,0.1); border: 1px solid var(--oxblood); padding: 10px 14px; border-radius: 6px; }
      `}</style>
    </div>
  );
}
