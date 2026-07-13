import { useState } from "react";

const TAG_RE = /\[(E\d+)\]/g;

function renderInline(text, citationMap, onCite) {
  const parts = [];
  let last = 0;
  let m;
  TAG_RE.lastIndex = 0;
  while ((m = TAG_RE.exec(text))) {
    if (m.index > last) parts.push(text.slice(last, m.index));
    const tag = m[1];
    const citation = citationMap[tag];
    parts.push(
      <button
        key={`${tag}-${m.index}`}
        className="cite-chip"
        onClick={() => onCite(citation, tag)}
        title={citation ? `${citation.document_id} p.${citation.page ?? "?"}` : "citation not found"}
      >
        {tag}
      </button>
    );
    last = m.index + m[0].length;
  }
  parts.push(text.slice(last));
  return parts;
}

function parseSections(markdown) {
  const lines = markdown.split("\n");
  const blocks = [];
  let current = null;
  for (const line of lines) {
    const heading = line.match(/^##\s+(.*)/);
    if (heading) {
      current = { heading: heading[1], paragraphs: [] };
      blocks.push(current);
    } else if (line.trim() && current) {
      current.paragraphs.push(line);
    } else if (line.trim() && !current) {
      current = { heading: null, paragraphs: [line] };
      blocks.push(current);
    }
  }
  return blocks;
}

export default function ReportViewer({ markdown, evidence }) {
  const [active, setActive] = useState(null);
  const citationMap = Object.fromEntries((evidence?.citations || []).map((c) => [c.tag, c]));
  const blocks = parseSections(markdown || "");

  return (
    <div className="report-viewer">
      {blocks.map((b, i) => (
        <section key={i} className="report-section">
          {b.heading && <h4>{b.heading}</h4>}
          {b.paragraphs.map((p, j) => (
            <p key={j}>{renderInline(p, citationMap, (c, tag) => setActive({ ...c, tag }))}</p>
          ))}
        </section>
      ))}

      {active && (
        <div className="cite-popover" role="dialog" onClick={() => setActive(null)}>
          <div className="cite-card" onClick={(e) => e.stopPropagation()}>
            <div className="cite-card-head">
              <span className="mono">[{active.tag}]</span>
              <button className="cite-close" onClick={() => setActive(null)}>
                ×
              </button>
            </div>
            <div className="cite-source mono">
              {active.document_id}
              {active.page ? `, p.${active.page}` : ""}
            </div>
            <blockquote>{active.quote || "citation data not found"}</blockquote>
          </div>
        </div>
      )}

      <style>{`
        .report-viewer { font-family: var(--serif); font-size: 15.5px; line-height: 1.75; color: var(--parchment); }
        .report-section { margin-bottom: 20px; }
        .report-section h4 { font-size: 16px; margin-bottom: 8px; color: var(--gold-bright); }
        .report-section p { margin: 0 0 10px; }
        .cite-chip {
          font-family: var(--mono);
          font-size: 11px;
          background: transparent;
          border: 1px solid var(--gold);
          color: var(--gold-bright);
          border-radius: 3px;
          padding: 0 5px;
          margin: 0 2px;
          cursor: pointer;
          vertical-align: 2px;
          transition: background 0.15s, transform 0.15s;
        }
        .cite-chip:hover { background: var(--gold); color: var(--ink); transform: translateY(-1px); }
        .cite-popover {
          position: fixed; inset: 0; background: rgba(10, 8, 5, 0.6);
          display: flex; align-items: center; justify-content: center; z-index: 200;
        }
        .cite-card {
          background: var(--surface-2); border: 1px solid var(--hairline); border-radius: 8px;
          padding: 18px 20px; max-width: 420px; box-shadow: 0 20px 60px rgba(0,0,0,0.5);
          animation: riseIn 0.2s ease both;
        }
        .cite-card-head { display: flex; justify-content: space-between; align-items: center; color: var(--gold-bright); margin-bottom: 10px; }
        .cite-close { background: none; border: none; color: var(--parchment-faint); font-size: 20px; cursor: pointer; line-height: 1; }
        .cite-source { color: var(--parchment-dim); font-size: 12px; margin-bottom: 8px; }
        blockquote { margin: 0; font-family: var(--serif); font-style: italic; color: var(--parchment); border-left: 2px solid var(--gold); padding-left: 12px; }
      `}</style>
    </div>
  );
}
