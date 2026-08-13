import { useState } from "react";

const MEDICAL_REVIEW_ACTIONS = [
  "provide_information",
  "assign",
  "request_information",
  "supplement_package",
  "reassign",
  "submit_response",
  "amend_response",
  "withdraw_response",
  "flag_conflict",
  "adjudicate",
  "cancel",
  "close",
  "reopen",
];

const splitList = (value) => value.split(/[\n,]/).map((entry) => entry.trim()).filter(Boolean);

function responseSubmission(request, response, form, action) {
  return {
    request_id: request.request_id,
    request_version_reviewed: request.request_version,
    issue_id: request.issue_id,
    issue_category: request.issue_category,
    decision_id: request.decision_id,
    request_config_version_reviewed: request.request_config_version,
    referral_policy_version_reviewed: request.referral_policy_version,
    reviewed_at: new Date(form.reviewedAt).toISOString(),
    evidence_locator_ids_reviewed: splitList(form.evidenceReviewed),
    interpretation: form.interpretation.trim(),
    basis: form.basis.trim(),
    uncertainty: form.uncertainty.trim(),
    alternative_interpretations: splitList(form.alternatives),
    additional_evidence_needed: splitList(form.additionalEvidence),
    downstream_adjustment_advice: form.downstreamAdvice.trim() || null,
    supersedes_response_id: action === "amend_response" ? response?.response_id || null : null,
    response_status: action === "amend_response" ? "amended" : "completed",
    attestation: form.attestation.trim(),
  };
}

function supplementedRequest(request, form) {
  const keys = [
    "issue_id", "issue_category", "question_scope", "suggested_specialty_code",
    "case_summary", "accident_summary", "timeline_observation_ids",
    "prior_condition_observation_ids", "prognostic_observation_ids",
    "included_issue_evidence_link_ids", "included_evidence_locator_ids",
    "omitted_supporting_links", "uncertainties", "source_coverage",
    "schema_version", "referral_policy_version",
  ];
  return Object.fromEntries([
    ...keys.map((key) => [key, request[key]]),
    ["question", form.supplementQuestion.trim() || request.question],
  ]);
}

function buildActionBody(action, item, request, response, form) {
  const reason = form.reason.trim() || undefined;
  if (action === "provide_information") return { data: { information: form.information.trim() }, reason };
  if (action === "assign" || action === "reassign") {
    return {
      data: {
        request_id: request.request_id,
        request_version: request.request_version,
        reviewer_actor_id: form.reviewerActorId.trim(),
        package_review_attestation: {
          reviewed_by_actor_id: "authenticated-operator",
          reviewed_at: new Date(form.reviewedAt).toISOString(),
          source_reinspection_confirmed: form.sourceReinspectionConfirmed,
          focused_question_confirmed: form.focusedQuestionConfirmed,
          no_verdict_confirmed: form.noVerdictConfirmed,
        },
      },
      reason,
    };
  }
  if (action === "request_information") {
    return { data: { request_id: request.request_id, request_version: request.request_version, information_required: form.information.trim() }, reason };
  }
  if (action === "supplement_package") {
    return {
      data: {
        request_id: request.request_id,
        request: supplementedRequest(request, form),
        source_reinspection: {
          performed: form.reinspectionPerformed,
          observation_ids: splitList(form.reinspectionObservationIds),
          evidence_locator_ids: splitList(form.reinspectionLocatorIds),
          result: form.reinspectionResult,
          unresolved_reason: form.reinspectionResult === "resolved" ? null : form.reason.trim(),
        },
      },
      reason,
    };
  }
  if (action === "submit_response" || action === "amend_response") {
    return {
      data: {
        response: responseSubmission(request, response, form, action),
        ...(action === "amend_response" ? { reason: form.reason.trim() } : {}),
      },
      reason,
    };
  }
  if (action === "withdraw_response") return { data: { response_id: response?.response_id }, reason };
  if (action === "flag_conflict") {
    return {
      data: {
        items: splitList(form.conflictPairs).map((entry) => {
          const [review_item_id, response_id] = entry.split(":").map((value) => value.trim());
          return { review_item_id, response_id };
        }),
      },
      reason,
    };
  }
  if (action === "adjudicate") return { data: { adjudication_id: item.current_adjudication_id, record: form.adjudicationRecord.trim() }, reason };
  if (action === "cancel") return { data: item.current_adjudication_id ? { adjudication_id: item.current_adjudication_id } : {}, reason };
  if (action === "close") return { data: null, reason };
  return { data: { decision_owner: form.decisionOwner, reason: form.reason.trim() }, reason };
}

const initialForm = {
  information: "",
  reviewerActorId: "",
  reviewedAt: "",
  evidenceReviewed: "",
  sourceReinspectionConfirmed: false,
  focusedQuestionConfirmed: false,
  noVerdictConfirmed: false,
  reinspectionPerformed: false,
  reinspectionObservationIds: "",
  reinspectionLocatorIds: "",
  supplementQuestion: "",
  reinspectionResult: "unresolved",
  interpretation: "",
  basis: "",
  uncertainty: "",
  alternatives: "",
  additionalEvidence: "",
  downstreamAdvice: "",
  attestation: "",
  conflictPairs: "",
  adjudicationRecord: "",
  decisionOwner: "human",
  reason: "",
};

function Field({ label, children }) {
  return <label className="medical-field"><span>{label}</span>{children}</label>;
}

export default function MedicalReviewResponseForm({ item, request, response, busy, onAction }) {
  const [action, setAction] = useState("submit_response");
  const [form, setForm] = useState(initialForm);
  const set = (key) => (event) => setForm((current) => ({ ...current, [key]: event.target.value }));
  const check = (key) => (event) => setForm((current) => ({ ...current, [key]: event.target.checked }));
  const submit = (event) => {
    event.preventDefault();
    onAction(action, buildActionBody(action, item, request, response, form));
  };
  const responseAction = action === "submit_response" || action === "amend_response";
  const assignmentAction = action === "assign" || action === "reassign";

  return (
    <form className="medical-action-form" onSubmit={submit}>
      <h4>Lifecycle action</h4>
      <Field label="Action">
        <select value={action} onChange={(event) => setAction(event.target.value)}>
          {MEDICAL_REVIEW_ACTIONS.map((value) => <option key={value} value={value}>{value.replaceAll("_", " ")}</option>)}
        </select>
      </Field>

      {(action === "provide_information" || action === "request_information") && (
        <Field label={action === "provide_information" ? "Information supplied" : "Additional information needed"}>
          <textarea required value={form.information} onChange={set("information")} />
        </Field>
      )}
      {assignmentAction && <>
        <Field label="Medical reviewer actor ID"><input required value={form.reviewerActorId} onChange={set("reviewerActorId")} /></Field>
        <Field label="Package reviewed at"><input type="datetime-local" required value={form.reviewedAt} onChange={set("reviewedAt")} /></Field>
        <Field label="Source reinspection confirmed"><input type="checkbox" required checked={form.sourceReinspectionConfirmed} onChange={check("sourceReinspectionConfirmed")} /></Field>
        <Field label="Focused question confirmed"><input type="checkbox" required checked={form.focusedQuestionConfirmed} onChange={check("focusedQuestionConfirmed")} /></Field>
        <Field label="No-verdict boundary confirmed"><input type="checkbox" required checked={form.noVerdictConfirmed} onChange={check("noVerdictConfirmed")} /></Field>
      </>}
      {action === "supplement_package" && <>
        <Field label="Updated focused question"><textarea value={form.supplementQuestion} onChange={set("supplementQuestion")} placeholder={request?.question} /></Field>
        <Field label="Source reinspection performed"><input type="checkbox" required checked={form.reinspectionPerformed} onChange={check("reinspectionPerformed")} /></Field>
        <Field label="Reinspected observation IDs"><textarea required value={form.reinspectionObservationIds} onChange={set("reinspectionObservationIds")} placeholder="One per line" /></Field>
        <Field label="Reinspected evidence locator IDs"><textarea required value={form.reinspectionLocatorIds} onChange={set("reinspectionLocatorIds")} placeholder="One per line" /></Field>
        <Field label="Source reinspection result"><select value={form.reinspectionResult} onChange={set("reinspectionResult")}><option value="resolved">resolved</option><option value="unresolved">unresolved</option><option value="insufficient_source">insufficient source</option><option value="blocked_upstream">blocked upstream</option></select></Field>
      </>}
      {responseAction && <>
        <Field label="Clinical review completed at"><input type="datetime-local" required value={form.reviewedAt} onChange={set("reviewedAt")} /></Field>
        <Field label="Evidence locator IDs actually reviewed"><textarea required value={form.evidenceReviewed} onChange={set("evidenceReviewed")} placeholder="One per line" /></Field>
        <Field label="Interpretation"><textarea required value={form.interpretation} onChange={set("interpretation")} /></Field>
        <Field label="Basis"><textarea required value={form.basis} onChange={set("basis")} /></Field>
        <Field label="Uncertainty"><textarea required value={form.uncertainty} onChange={set("uncertainty")} /></Field>
        <Field label="Alternative interpretations"><textarea value={form.alternatives} onChange={set("alternatives")} placeholder="One per line" /></Field>
        <Field label="Additional evidence needed"><textarea value={form.additionalEvidence} onChange={set("additionalEvidence")} placeholder="One per line" /></Field>
        <Field label="Downstream adjustment advice"><textarea value={form.downstreamAdvice} onChange={set("downstreamAdvice")} /></Field>
        <Field label="Attestation"><textarea required value={form.attestation} onChange={set("attestation")} /></Field>
      </>}
      {action === "flag_conflict" && <Field label="Conflicting item and response pairs"><textarea required value={form.conflictPairs} onChange={set("conflictPairs")} placeholder="MRI_0001:MRR_0001-R01, one per line" /></Field>}
      {action === "adjudicate" && <Field label="Adjudication record"><textarea required value={form.adjudicationRecord} onChange={set("adjudicationRecord")} /></Field>}
      {action === "reopen" && <Field label="Decision owner"><select value={form.decisionOwner} onChange={set("decisionOwner")}><option value="human">human</option><option value="policy">policy</option></select></Field>}
      {(action === "close" || action === "reopen" || action === "cancel" || action === "flag_conflict" || action === "amend_response" || action === "supplement_package" || action === "reassign" || action === "withdraw_response") && (
        <Field label="Reason"><textarea required value={form.reason} onChange={set("reason")} /></Field>
      )}
      <button className="medical-primary" type="submit" disabled={busy || !item || ((responseAction || assignmentAction || action === "supplement_package" || action === "request_information") && !request)}>
        {busy ? "Applying…" : "Apply through DAO"}
      </button>
    </form>
  );
}
