function observationValue(observation) {
  if (!observation) return "Unavailable";
  if (observation.value_state !== "asserted") return observation.state_reason || observation.value_state;
  if (observation.text_value) return observation.text_value;
  if (observation.coded_value) {
    return observation.coded_value.display || observation.coded_value.code || "Coded value";
  }
  if (observation.temporal_value) {
    return Object.entries(observation.temporal_value)
      .map(([key, value]) => `${key.replaceAll("_", " ")}: ${value}`)
      .join(" · ");
  }
  if (observation.numeric_value) return `${observation.numeric_value.number ?? ""} ${observation.numeric_value.unit_code ?? observation.numeric_value.raw_unit ?? ""}`.trim();
  if (observation.quantity_value) return `${observation.quantity_value.count} ${observation.quantity_value.unit_code ?? observation.quantity_value.raw_unit ?? ""}`.trim();
  return "Recorded observation";
}

function EvidenceColumn({ title, entries, tone, onOpenEvidence }) {
  return (
    <section className={`medical-evidence-column ${tone}`}>
      <h5>{title}</h5>
      {entries.length === 0 && <p className="medical-empty">None recorded.</p>}
      {entries.map((entry) => (
        <article className="medical-evidence-card" key={`${entry.observation_id}-${entry.relation}`}>
          <strong>{entry.variable_label}</strong>
          <p>{observationValue(entry)}</p>
          <small>{entry.reason}</small>
          <div className="medical-chip-row">
            {(entry.evidence || []).map((locator) => (
              <button
                className="medical-link-button"
                key={locator.locator_id}
                type="button"
                onClick={() => onOpenEvidence?.(locator.locator_id)}
              >
                {locator.locator_id}
              </button>
            ))}
          </div>
        </article>
      ))}
    </section>
  );
}

export default function MedicalVariablesPanel({ workspace, onOpenEvidence }) {
  return (
    <div className="medical-variables-panel">
      <section className="medical-section">
        <h4>Medical timeline</h4>
        <div className="medical-timeline">
          {workspace.timeline.map((entry, index) => (
            <article className="medical-timeline-entry" key={entry.observation_id}>
              <span className="medical-timeline-index mono">{String(index + 1).padStart(2, "0")}</span>
              <div>
                <strong>{entry.variable_label}</strong>
                <p>{observationValue(entry)}</p>
                <small className="mono">{entry.observation_id}</small>
              </div>
            </article>
          ))}
        </div>
      </section>

      <section className="medical-section">
        <h4>Contradictions</h4>
        {workspace.conflicts.length === 0 && <p className="medical-empty">No canonical contradiction group.</p>}
        {workspace.conflicts.map((conflict) => (
          <article className="medical-conflict" key={conflict.contradiction_group_id}>
            <header><span className="mono">{conflict.conflict_id}</span></header>
            <div className="medical-conflict-grid">
              {conflict.observations.map((entry) => (
                <div key={entry.observation_id}>
                  <strong>{entry.variable_label}</strong>
                  <p>{observationValue(entry)}</p>
                  <small className="mono">{entry.observation_id}</small>
                </div>
              ))}
            </div>
          </article>
        ))}
      </section>

      <div className="medical-evidence-grid">
        <EvidenceColumn title="Relevant evidence" entries={workspace.evidence.relevant} tone="relevant" onOpenEvidence={onOpenEvidence} />
        <EvidenceColumn title="Countervailing evidence" entries={workspace.evidence.countervailing} tone="countervailing" onOpenEvidence={onOpenEvidence} />
      </div>
    </div>
  );
}
