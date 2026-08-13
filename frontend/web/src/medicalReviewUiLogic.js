const COUNTERVAILING_RELATIONS = new Set(["weakens", "conflicts", "missing_expected"]);

function indexObservations(variables) {
  const observations = new Map();
  for (const variable of variables?.variables || []) {
    for (const observation of variable.observations || []) {
      observations.set(observation.observation_id, {
        ...observation,
        variable_id: variable.variable_id,
        variable_label: variable.label,
      });
    }
  }
  return observations;
}

function latest(records) {
  return records?.length ? records[records.length - 1] : null;
}

export function currentRequestRevision(ledger, reviewItemId) {
  const item = (ledger?.review_items || []).find(
    (entry) => entry.review_item_id === reviewItemId,
  );
  const record = item?.current_request_id
    ? (item.requests || []).find((entry) => entry.request_id === item.current_request_id)
    : null;
  return latest(record?.versions)?.medical_variables_revision?.sha256 || null;
}

export function evidenceMatchesCoordinate(payload, coordinate) {
  return payload?.case_id === coordinate.caseId
    && payload?.review_item_id === coordinate.reviewItemId
    && payload?.request_id === coordinate.requestId
    && payload?.request_version === coordinate.requestVersion
    && payload?.revision_sha === coordinate.revisionSha
    && payload?.locator?.locator_id === coordinate.locatorId;
}

export function caseSnapshotFor(snapshot, caseId) {
  return snapshot?.caseId === caseId ? snapshot : null;
}

export function medicalOperationSignature(action, body, events = []) {
  return JSON.stringify({
    action,
    body,
    lifecycleHead: events.at(-1)?.event_id || null,
  });
}

export function buildMedicalWorkspace(
  variables, ledger, selectedReviewItemId = null, runState = null,
) {
  const observations = indexObservations(variables);
  const timeline = (variables?.timeline_observation_ids || [])
    .map((id) => observations.get(id))
    .filter(Boolean);
  const conflicts = (variables?.contradiction_groups || []).map((group) => ({
    ...group,
    observations: group.observation_ids
      .map((id) => observations.get(id))
      .filter(Boolean),
  }));

  const items = ledger?.review_items || [];
  const item = items.find(
    (entry) => entry.review_item_id === selectedReviewItemId,
  ) || items[0] || null;
  const issue = item
    ? (variables?.medical_issues || []).find((entry) => entry.issue_id === item.issue_id) || null
    : (variables?.medical_issues || [])[0] || null;
  const referral = latest(item?.decisions) || null;
  const requestRecord = item?.current_request_id
    ? (item.requests || []).find((record) => record.request_id === item.current_request_id) || null
    : null;
  const request = latest(requestRecord?.versions) || null;
  const response = requestRecord?.current_response_id
    ? (requestRecord.responses || []).find(
      (entry) => entry.response_id === requestRecord.current_response_id,
    ) || null
    : null;
  const requestHistory = (item?.requests || []).flatMap(
    (record) => record.versions || [],
  );
  const responseHistory = (item?.requests || []).flatMap(
    (record) => record.responses || [],
  );
  const assignmentHistory = (item?.requests || []).flatMap(
    (record) => record.assignments || [],
  );
  const sourceReinspectionHistory = item?.source_reinspection_records || [];
  const allLedgerWaits = ledger?.wait_episodes || [];
  const ledgerWaits = allLedgerWaits.filter(
    (wait) => wait.review_item_id === item?.review_item_id
      || (wait.related_review_item_ids || []).includes(item?.review_item_id),
  );
  const projectedWaits = new Map(
    (runState?.human_input_status || [])
      .filter((wait) => wait.human_input_id)
      .map((wait) => [wait.human_input_id, wait]),
  );
  const waits = ledgerWaits.map((wait) => ({
    ...wait,
    ledger_status: wait.status,
    status: projectedWaits.get(wait.human_input_id)?.status || "projection_missing",
    run_state_projection: projectedWaits.get(wait.human_input_id) || null,
  }));
  const ledgerWaitIds = new Set(allLedgerWaits.map((wait) => wait.human_input_id));
  const waitProjectionErrors = [
    ...allLedgerWaits
      .filter((wait) => !projectedWaits.has(wait.human_input_id))
      .map((wait) => `Missing run-state projection for ${wait.human_input_id}`),
    ...[...projectedWaits.keys()]
      .filter((id) => !ledgerWaitIds.has(id))
      .map((id) => `Run-state projection ${id} has no ledger wait episode`),
  ];
  const includedLocators = new Set(request?.included_evidence_locator_ids || []);

  const evidence = { relevant: [], countervailing: [] };
  for (const link of issue?.evidence_links || []) {
    const observation = observations.get(link.observation_id);
    if (!observation) continue;
    const pinnedEvidence = (observation.evidence || []).filter(
      (locator) => includedLocators.has(locator.locator_id),
    );
    if (!pinnedEvidence.length) continue;
    const entry = {
      ...observation,
      evidence: pinnedEvidence,
      relation: link.relation,
      reason: link.reason,
    };
    if (COUNTERVAILING_RELATIONS.has(link.relation)) {
      evidence.countervailing.push(entry);
    } else {
      evidence.relevant.push(entry);
    }
  }

  return {
    items,
    item,
    issue,
    timeline,
    conflicts,
    referral,
    request,
    response,
    requestHistory,
    responseHistory,
    assignmentHistory,
    sourceReinspectionHistory,
    waits,
    waitProjectionErrors,
    evidence,
    events: (ledger?.events || []).filter(
      (event) => event.review_item_id === item?.review_item_id,
    ),
  };
}

export function planActionCompletion(startedGeneration, currentGeneration, status) {
  const isCurrent = startedGeneration === currentGeneration;
  return {
    isCurrent,
    releaseBusy: isCurrent,
    reconcile: !isCurrent && status !== "failed",
    projectionPending: status === "committed_projection_pending",
  };
}
