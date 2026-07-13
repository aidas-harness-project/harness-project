function titleCase(key) {
  return key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function Value({ v }) {
  if (v === null || v === undefined) return <span className="muted">--</span>;
  if (typeof v === "boolean") return <span>{v ? "yes" : "no"}</span>;
  if (typeof v === "number") return <span className="mono">{v}</span>;
  if (Array.isArray(v)) {
    if (v.length === 0) return <span className="muted">none</span>;
    return (
      <ul className="contract-list">
        {v.map((item, i) => (
          <li key={i}>
            {typeof item === "object" && item !== null ? <Fields data={item} /> : <Value v={item} />}
          </li>
        ))}
      </ul>
    );
  }
  if (typeof v === "object") return <Fields data={v} />;
  return <span>{String(v)}</span>;
}

function Fields({ data }) {
  const entries = Object.entries(data).filter(([k]) => !k.startsWith("_"));
  return (
    <dl className="contract-fields">
      {entries.map(([k, v]) => (
        <div key={k} className={`contract-row${k === "confidence" ? " confidence" : ""}${k === "review_required" && v ? " flagged" : ""}`}>
          <dt>{titleCase(k)}</dt>
          <dd>
            <Value v={v} />
          </dd>
        </div>
      ))}
      <style>{`
        .contract-fields { margin: 0; }
        .contract-row {
          display: grid;
          grid-template-columns: minmax(140px, 30%) 1fr;
          gap: 12px;
          padding: 5px 0;
          border-bottom: 1px solid var(--hairline);
          font-size: 13.5px;
        }
        .contract-row dt { color: var(--parchment-faint); }
        .contract-row dd { margin: 0; }
        .contract-row.confidence dd { font-family: var(--mono); color: var(--gold-bright); }
        .contract-row.flagged { background: rgba(219, 165, 69, 0.06); }
        .contract-list { margin: 0; padding-left: 18px; }
        .contract-list > li { margin: 6px 0; }
        .muted { color: var(--parchment-faint); font-style: italic; }
      `}</style>
    </dl>
  );
}

export default function ContractView({ data }) {
  if (!data) return null;
  return <Fields data={data} />;
}
