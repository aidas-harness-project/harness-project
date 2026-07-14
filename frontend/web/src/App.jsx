import { useEffect, useState, useCallback } from "react";
import Sidebar from "./components/Sidebar";
import StageDetail from "./components/StageDetail";
import RunBanner from "./components/RunBanner";
import { PHASE_1, PHASE_2, TRIGGERED, ALL_STAGES } from "./pipelineDefinition";
import { api } from "./api";

function useCaseData(caseId) {
  const [runState, setRunState] = useState(null);
  const [ledgers, setLedgers] = useState(null);
  const [ocrReview, setOcrReview] = useState(null);
  const [error, setError] = useState(null);

  const reload = useCallback(() => {
    if (!caseId) return;
    setError(null);
    Promise.all([
      api.runState(caseId),
      api.ledgers(caseId),
      api.ocrReview(caseId).catch(() => null), // never let the P8 queue take the whole view down
    ])
      .then(([rs, lg, ocr]) => {
        setRunState(rs);
        setLedgers(lg);
        setOcrReview(ocr);
      })
      .catch((e) => setError(e.message));
  }, [caseId]);

  useEffect(reload, [reload]);
  return { runState, ledgers, ocrReview, error, reload };
}

export default function App() {
  const [cases, setCases] = useState([]);
  const [current, setCurrent] = useState(null);
  const [selectedStage, setSelectedStage] = useState(PHASE_1[0].key);
  const { runState, ledgers, ocrReview, error, reload } = useCaseData(current);

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

  const stageDef = ALL_STAGES.find((s) => s.key === selectedStage);
  let phaseLabel, indexWithinPhase;
  if (PHASE_1.some((s) => s.key === selectedStage)) {
    phaseLabel = "Phase 1";
    indexWithinPhase = PHASE_1.findIndex((s) => s.key === selectedStage);
  } else if (PHASE_2.some((s) => s.key === selectedStage)) {
    phaseLabel = "Phase 2";
    indexWithinPhase = PHASE_2.findIndex((s) => s.key === selectedStage);
  } else {
    phaseLabel = "Triggered";
    indexWithinPhase = TRIGGERED.findIndex((s) => s.key === selectedStage);
  }

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
        ocrReview={ocrReview}
        onRefresh={reload}
        onCaseListChanged={refreshCaseList}
      />

      <main className="app-main">
        {!current && <p className="muted">Select a case to begin.</p>}
        {error && <p className="error-banner">{error}</p>}
        {current && <RunBanner caseId={current} onActivity={reload} />}
        {current && runState && stageDef && (
          <StageDetail
            stageDef={stageDef}
            index={indexWithinPhase}
            phaseLabel={phaseLabel}
            runState={runState}
            ledgers={ledgers}
            ocrReview={ocrReview}
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
