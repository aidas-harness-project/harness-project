const recordLabel = (key) => key.replaceAll("_", " ");

export function StructuredMedicalRecord({ value }) {
  if (value === null || value === undefined || value === "") {
    return <span className="medical-provenance-empty">Not recorded</span>;
  }
  if (typeof value === "boolean") return <span>{value ? "Yes" : "No"}</span>;
  if (Array.isArray(value)) {
    if (!value.length) return <span className="medical-provenance-empty">None recorded</span>;
    return <ul className="medical-provenance-array">{value.map((entry, index) => (
      <li key={`${index}-${typeof entry === "object" ? "record" : entry}`}><StructuredMedicalRecord value={entry} /></li>
    ))}</ul>;
  }
  if (typeof value === "object") {
    return <dl className="medical-provenance-record">{Object.entries(value).map(([key, entry]) => (
      <div key={key}><dt>{recordLabel(key)}</dt><dd><StructuredMedicalRecord value={entry} /></dd></div>
    ))}</dl>;
  }
  return <span>{String(value)}</span>;
}

export default function MedicalReviewPackage({ item, request, requestHistory, responseHistory, assignmentHistory, sourceReinspectionHistory, evidence, onOpenEvidence }) {
  const hasHistory = [
    requestHistory,
    responseHistory,
    assignmentHistory,
    sourceReinspectionHistory,
  ].some((records) => records?.length);
  if (!request && !hasHistory) return <p className="medical-empty">No focused request has been published.</p>;
  return (
    <section className="medical-package">
      {request ? <>
        <div className="medical-package-heading">
          <div>
            <span className="mono">{request.request_id}</span>
            <h4>Focused request</h4>
          </div>
          <span className="medical-state">Request version {request.request_version}</span>
        </div>
        <p className="medical-question">{request.question}</p>
        <dl className="medical-facts">
          <div><dt>State</dt><dd>{item?.state || "unknown"}</dd></div>
          <div><dt>Specialty</dt><dd>{request.suggested_specialty_code}</dd></div>
          <div><dt>Revision</dt><dd className="mono">{request.medical_variables_revision?.sha256 || "unknown"}</dd></div>
        </dl>
        <details className="medical-canonical-record"><summary>Complete canonical request record</summary><StructuredMedicalRecord value={request} /></details>
        <div className="medical-package-copy">
          <div><strong>Case summary</strong><p>{request.case_summary}</p></div>
          <div><strong>Accident summary</strong><p>{request.accident_summary}</p></div>
          <div><strong>Uncertainties</strong><ul>{(request.uncertainties || []).map((value) => <li key={value}>{value}</li>)}</ul></div>
        </div>
        <div>
          <h5>Evidence package</h5>
          <div className="medical-chip-row">
            {(request.included_evidence_locator_ids || []).map((locatorId) => (
              <button className="medical-link-button" key={locatorId} type="button" onClick={() => onOpenEvidence(locatorId)}>
                {locatorId}
              </button>
            ))}
          </div>
        </div>
        <div>
          <h5>Source coverage limitations</h5>
          <ul>
            {(request.source_coverage || []).map((entry) => <li key={entry}>{entry}</li>)}
          </ul>
        </div>
      </> : <p className="medical-empty">No focused request is currently active. Canonical history remains available below.</p>}
      <details>
        <summary>Request and response history</summary>
        <ul>
          {(requestHistory || []).map((entry) => <li key={`${entry.request_id}-${entry.request_version}`}>{entry.request_id} · request version {entry.request_version}<details><summary>Complete canonical request version</summary><StructuredMedicalRecord value={entry} /></details></li>)}
          {(assignmentHistory || []).map((entry) => <li key={entry.assignment_id}>{entry.assignment_id} · reviewer {entry.reviewer?.display_name}<details><summary>Complete canonical assignment record</summary><StructuredMedicalRecord value={entry} /></details></li>)}
          {(responseHistory || []).map((entry) => <li key={entry.response_id}>{entry.response_id} · response version {entry.response_version} · {entry.response_status}<details><summary>Complete canonical response record</summary><StructuredMedicalRecord value={entry} /></details></li>)}
          {(sourceReinspectionHistory || []).map((entry) => <li key={entry.source_reinspection_id}>{entry.source_reinspection_id} · {entry.result}<details><summary>Complete canonical reinspection record</summary><StructuredMedicalRecord value={entry} /></details></li>)}
        </ul>
      </details>
      {evidence && (
        <aside className="medical-evidence-detail">
          <span className="mono">{evidence.locator?.locator_id}</span>
          <strong>{evidence.locator?.document_id || evidence.locator?.source_reference?.document_id}</strong>
          <p>{evidence.locator?.quote || evidence.locator?.source_reference?.quote || "Evidence location verified in the pinned request package."}</p>
          <details><summary>Complete canonical evidence record</summary><StructuredMedicalRecord value={evidence} /></details>
        </aside>
      )}
    </section>
  );
}
