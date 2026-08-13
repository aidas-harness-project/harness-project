import { useEffect, useRef, useState, useCallback } from "react";
import Sidebar from "./components/Sidebar";
import StageDetail from "./components/StageDetail";
import RunBanner from "./components/RunBanner";
import { PHASE_1, PHASE_2, TRIGGERED, ALL_STAGES } from "./pipelineDefinition";
import { api } from "./api";
import { caseSnapshotFor } from "./medicalReviewUiLogic";

function useCaseData(caseId) {
  const [snapshot, setSnapshot] = useState(null);
  const generationRef = useRef(0);

  const reload = useCallback(() => {
    const generation = ++generationRef.current;
    if (!caseId) {
      setSnapshot(null);
      return;
    }
    setSnapshot((current) => current?.caseId === caseId
      ? { ...current, error: null }
      : current);
    Promise.all([
      api.runState(caseId),
      api.ledgers(caseId),
      api.ocrReview(caseId).catch(() => null), // never let the P8 queue take the whole view down
    ])
      .then(([rs, lg, ocr]) => {
        if (generation !== generationRef.current) return;
        setSnapshot({ caseId, runState: rs, ledgers: lg, ocrReview: ocr, error: null });
      })
      .catch((e) => {
        if (generation !== generationRef.current) return;
        setSnapshot((current) => ({
          caseId,
          runState: current?.caseId === caseId ? current.runState : null,
          ledgers: current?.caseId === caseId ? current.ledgers : null,
          ocrReview: current?.caseId === caseId ? current.ocrReview : null,
          error: e.message,
        }));
      });
  }, [caseId]);

  useEffect(() => {
    reload();
    return () => {
      generationRef.current += 1;
    };
  }, [reload]);
  const currentSnapshot = caseSnapshotFor(snapshot, caseId);
  return {
    runState: currentSnapshot?.runState || null,
    ledgers: currentSnapshot?.ledgers || null,
    ocrReview: currentSnapshot?.ocrReview || null,
    error: currentSnapshot?.error || null,
    reload,
  };
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
        mutationsAvailable={api.mutationsAvailable}
      />

      <main className="app-main">
        {!current && <p className="muted">Select a case to begin.</p>}
        {error && <p className="error-banner">{error}</p>}
        {current && <RunBanner caseId={current} onActivity={reload} />}
        {current && runState && stageDef && (
          <StageDetail
            key={`${current}:${stageDef.key}`}
            stageDef={stageDef}
            index={indexWithinPhase}
            phaseLabel={phaseLabel}
            runState={runState}
            ledgers={ledgers}
            ocrReview={ocrReview}
            caseId={current}
            onLedgersChanged={reload}
            readOnly={!api.mutationsAvailable}
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
