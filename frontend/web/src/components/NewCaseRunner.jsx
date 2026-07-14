import { useEffect, useRef, useState } from "react";
import { api } from "../api";

export default function NewCaseRunner({ onOpenCase, onCaseListChanged }) {
  const [open, setOpen] = useState(false);
  const [caseId, setCaseId] = useState("");
  const [files, setFiles] = useState([]);
  const [phase, setPhase] = useState("idle"); // idle | uploading | launching | running | done | error
  const [message, setMessage] = useState("");
  const pollRef = useRef(null);

  useEffect(() => () => clearInterval(pollRef.current), []);

  function pickFiles(e) {
    setFiles(Array.from(e.target.files || []));
  }

  async function handleRun() {
    if (!/^CASE_[A-Za-z0-9_-]+$/.test(caseId)) {
      setMessage("case ID must look like CASE_<something>, e.g. CASE_010");
      setPhase("error");
      return;
    }
    if (!files.length) {
      setMessage("choose at least one document");
      setPhase("error");
      return;
    }
    try {
      setPhase("uploading");
      setMessage(`staging ${files.length} file(s)…`);
      await api.uploadDocuments(caseId, files);

      setPhase("launching");
      setMessage("launching Claude (scoped tool access, no permission bypass)…");
      const launch = await api.runCase(caseId);
      setMessage(`running as pid ${launch.pid} -- this can take a while. Polling status every 5s.`);
      setPhase("running");
      onCaseListChanged?.();

      pollRef.current = setInterval(async () => {
        const status = await api.runStatus(caseId).catch(() => ({ status: "unknown" }));
        onCaseListChanged?.();
        if (status.status !== "running") {
          clearInterval(pollRef.current);
          if (status.status === "crashed") {
            setPhase("error");
            setMessage(`run CRASHED (exit code ${status.exit_code}) -- open ${caseId} to see the log and where it stopped.`);
          } else if (status.status === "finished") {
            setPhase("done");
            setMessage(`finished cleanly. Check the sidebar for ${caseId}.`);
          } else {
            setPhase("error");
            setMessage(`run ended with status "${status.status}" -- open ${caseId} and check the run log.`);
          }
          onOpenCase?.(caseId);
        }
      }, 5000);
    } catch (e) {
      setPhase("error");
      setMessage(e.message);
    }
  }

  return (
    <div className="new-case-runner">
      <button className="new-case-toggle" onClick={() => setOpen((o) => !o)}>
        {open ? "× close" : "+ new case run"}
      </button>

      {open && (
        <div className="new-case-form">
          <label>
            Case ID
            <input value={caseId} onChange={(e) => setCaseId(e.target.value.toUpperCase())} placeholder="CASE_010" />
          </label>
          <label className="file-drop">
            <input type="file" multiple onChange={pickFiles} />
            {files.length ? `${files.length} file(s) selected` : "choose documents"}
          </label>
          <button
            className="run-btn"
            onClick={handleRun}
            disabled={phase === "uploading" || phase === "launching" || phase === "running"}
          >
            {phase === "running" ? "Running…" : "Run"}
          </button>
          {message && <p className={`runner-msg ${phase}`}>{message}</p>}
        </div>
      )}

      <style>{`
        .new-case-runner { padding: 14px 20px; border-bottom: 1px solid var(--hairline); }
        .new-case-toggle {
          width: 100%; background: none; border: 1px dashed var(--hairline); color: var(--gold-bright);
          font-family: var(--mono); font-size: 12px; padding: 8px; border-radius: 5px; cursor: pointer;
        }
        .new-case-toggle:hover { border-color: var(--gold); }
        .new-case-form { display: flex; flex-direction: column; gap: 10px; margin-top: 12px; }
        .new-case-form label { display: flex; flex-direction: column; gap: 5px; font-size: 11px; color: var(--parchment-faint); text-transform: uppercase; letter-spacing: 0.06em; }
        .new-case-form input[type="text"], .new-case-form input:not([type]) {
          background: var(--surface); border: 1px solid var(--hairline); color: var(--parchment);
          padding: 7px 10px; border-radius: 4px; font-family: var(--mono); font-size: 13px; text-transform: none;
        }
        .file-drop {
          position: relative; border: 1px dashed var(--hairline); border-radius: 4px; padding: 10px;
          text-align: center; cursor: pointer; font-size: 12px; color: var(--parchment-dim);
        }
        .file-drop input { position: absolute; inset: 0; opacity: 0; cursor: pointer; }
        .run-btn {
          background: var(--gold); border: none; color: var(--ink); font-weight: 600;
          padding: 9px; border-radius: 5px; cursor: pointer; font-family: var(--sans); font-size: 13px;
        }
        .run-btn:disabled { opacity: 0.6; cursor: wait; }
        .runner-msg { font-size: 11.5px; color: var(--parchment-dim); margin: 0; line-height: 1.5; }
        .runner-msg.error { color: var(--oxblood); }
        .runner-msg.done { color: var(--sage); }
      `}</style>
    </div>
  );
}
