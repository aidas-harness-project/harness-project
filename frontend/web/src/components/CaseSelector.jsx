export default function CaseSelector({ cases, current, onSelect }) {
  if (!cases.length) return <span className="muted">No cases found under outputs/</span>;
  return (
    <div className="case-selector">
      {cases.map((c) => (
        <button key={c} className={`case-pill${c === current ? " active" : ""}`} onClick={() => onSelect(c)}>
          {c}
        </button>
      ))}
      <style>{`
        .case-selector { display: flex; gap: 8px; flex-wrap: wrap; }
        .case-pill {
          font-family: var(--mono); font-size: 12.5px; letter-spacing: 0.03em;
          background: var(--surface); border: 1px solid var(--hairline); color: var(--parchment-dim);
          padding: 6px 14px; border-radius: 20px; cursor: pointer; transition: all 0.15s;
        }
        .case-pill:hover { border-color: var(--gold); color: var(--parchment); }
        .case-pill.active { background: var(--gold); border-color: var(--gold); color: var(--ink); font-weight: 600; }
        .muted { color: var(--parchment-faint); font-style: italic; }
      `}</style>
    </div>
  );
}
