"""Selective Claim Analysis driver (routing config v0.1).

**The only claim-analysis driver.** It replaced `run_claim_analysis.py`'s
read-everything CP1, which was deleted 2026-08-20 once this lane was activated
and verified -- so this is no longer one of two spines behind a flag. It reads
documents **per field, in priority order, and stops at the first trusted
value**, instead of handing every claim-side document to one extraction call.

`behavior_enabled` survives the deletion as the governance record carrying the
activation approval; `require_enabled` still refuses a config that turns it
off, which now HALTS the stage rather than selecting the old spine.

What it owns:

* the wave loop (A, then B) over `claim_analysis_selection`'s plans,
* one provider call per (field-group, document) read -- never one per case,
* the stop rule and the single extra comparison a critical field may buy,
* assembling `claim_analysis_result.json` and `claim_analysis_trace.json`.

What it deliberately does NOT own:

* **Canonical medical authority.** This lane does not claim it. Every field is
  read from a case document and published as `source_document_extraction` with
  that document's exact quote and character range. It is NOT a projection of
  `medical_variables.json`: the PoC resolves no field id to a canonical
  variable id, so calling the output a projection would assert a binding that
  was never computed. `medical_projection_status: not_configured` says so in
  the contract itself.
* **Conflict adjudication.** A disagreement becomes a `conflict_candidate` with
  both observations preserved and no canonical value. It is NOT written to the
  P6 ledger here -- `consistency_check` verifies candidates and owns ledger
  entry creation. Writing them here is what would make `claim_analysis`
  conflict-gate itself (it is in `dao.CONFLICT_GATED_STAGES`).
* **Run-state.** T13: the orchestrator owns `update-run-state`/`finalize-stage`.
* **Eligibility and amounts.** Out of scope per the routing config.

Governance: case data is read only through DAO subcommands, every citation is
verified against served page text before anything is persisted, and the DAO
re-verifies exact ranges and the medical revision at write time.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, MutableMapping, Sequence

import claim_analysis_additional as additional_mod
import claim_analysis_case_types as case_types_mod
import claim_analysis_extraction as extraction
import claim_analysis_policy_links as policy_link_builder
import trace as trace_mod
from llm_providers import add_provider_args, build_provider, parse_provider_config
import claim_analysis_selection as selection
import medical_document_routing as routing

ROOT = Path(__file__).resolve().parent.parent
DAO = ROOT / "tools" / "dao.py"

STAGE = "claim_analysis"
COMPONENT = "claim-analysis"
RESULT_CONTRACT = "claim_analysis_result.json"
RESULT_SCHEMA = "claim_analysis_result.schema.json"
TRACE_CONTRACT = "claim_analysis_trace.json"
TRACE_SCHEMA = "claim_analysis_trace.schema.json"
VERSION = "claim_analysis_selective.v0.1"

# The one domain whose findings this stage owns as claim-side facts: accident
# circumstances, filing information, and case-type triggers. Everything else is
# read out of a medical document and published as source_document_extraction.
# Kept as an allow-list of the native domain rather than a list of medical ones
# so a domain added later defaults to "extracted from a document", which is the
# claim that needs no extra standing.
NATIVE_DOMAINS = frozenset({"event_timeline"})


# --------------------------------------------------------------- DAO edge --

def _dao_json(args: list[str], *, allow_missing: bool = False) -> dict | None:
    proc = subprocess.run([sys.executable, str(DAO), *args], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8")
    if proc.returncode:
        if allow_missing and "NOT_FOUND:" in (proc.stdout or ""):
            return None
        raise RuntimeError((proc.stdout or proc.stderr or "DAO command failed").strip())
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"DAO command returned non-JSON output: {args[0]}") from exc


def _dao_write(args: list[str]) -> None:
    proc = subprocess.run([sys.executable, str(DAO), *args], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8")
    if proc.returncode:
        raise RuntimeError((proc.stdout or proc.stderr or "DAO write failed").strip())


def _temp_json(value: Mapping[str, Any]) -> Path:
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".json", encoding="utf-8", delete=False)
    try:
        json.dump(value, handle, ensure_ascii=False)
    finally:
        handle.close()
    return Path(handle.name)


# ------------------------------------------------------- document intake --

def classified_documents(manifest: Mapping[str, Any]) -> list[selection.DocumentRef]:
    """Stage 2's fine-grained classifications, as the planner's document view.

    A document with no `medical_classification` block is NOT assumed
    non-medical: the block's own schema says its absence means the classifier
    has not run. Such a document carries `kind=None` and is therefore never
    routed -- it cannot silently stand in for a form kind it was never typed as.
    """
    documents: list[selection.DocumentRef] = []
    for entry in manifest.get("documents") or []:
        if not isinstance(entry, dict):
            continue
        doc_id = entry.get("document_id")
        if not isinstance(doc_id, str):
            continue
        # Carried alongside the medical kind so a route may name a coarse type
        # under `non_medical_sources`. It never substitutes for a medical kind.
        doc_type = entry.get("document_type")
        block = entry.get("medical_classification")
        if not isinstance(block, dict):
            documents.append(selection.DocumentRef(
                doc_id, None, document_type=doc_type))
            continue
        status = block.get("status")
        documents.append(selection.DocumentRef(
            document_id=doc_id,
            kind=block.get("kind") if status in {
                "deterministic_title", "llm_classified"
            } else None,
            ambiguous=status == "ambiguous",
            document_type=doc_type,
        ))
    return sorted(documents, key=lambda ref: ref.document_id)


def _page_text_index(bundle: Mapping[str, Any]) -> dict[tuple[str, int], str]:
    return {
        (document["document_id"], page["page"]): page["text"]
        for document in bundle.get("documents") or []
        for page in document.get("pages") or []
    }


# --------------------------------------------------- evidence verification --

def locate_exact(text: str, quote: str) -> tuple[int, int] | None:
    """The exact character range of `quote` in `text`, if it occurs exactly once.

    Ambiguity is refused rather than resolved: a quote occurring twice cannot
    say which occurrence it means, and `strict_evidence_reference` exists
    precisely because quote equality is not a binding relation. Returning None
    makes the caller drop the observation instead of guessing an offset the DAO
    would later have to catch.
    """
    if not quote:
        return None
    first = text.find(quote)
    if first < 0:
        return None
    if text.find(quote, first + 1) >= 0:
        return None
    return first, first + len(quote)


def build_reference(
    document_id: str,
    page: int,
    quote: str,
    page_text: str,
) -> dict | None:
    located = locate_exact(page_text, quote)
    if located is None:
        return None
    start, end = located
    return {
        "document_id": document_id,
        "page": page,
        "quote": quote,
        "start_char": start,
        "end_char": end,
    }


# ------------------------------------------------------------ extraction --

class FieldExtractionOutcome:
    """One field's settled state after its read ladder ran.

    Defaults to `unavailable`/`not_mentioned`: before any source has spoken,
    the honest verdict is that nothing was established, and the reason is that
    nothing mentioned it. A field only leaves that state by evidence.
    """

    __slots__ = ("field_id", "domain_code", "grade", "status", "observations",
                 "selected_ids", "stop_reason", "reason", "unavailable_reason",
                 "documents_read", "comparisons")

    def __init__(self, *, field_id: str, domain_code: str, grade: str) -> None:
        self.field_id = field_id
        self.domain_code = domain_code
        self.grade = grade
        self.status = "unavailable"
        self.observations: list[dict] = []
        self.selected_ids: list[str] = []
        self.stop_reason = "sources_exhausted"
        self.reason = "라우팅 우선순위상의 어떤 출처도 이 항목을 기재하지 않았습니다"
        self.unavailable_reason = "not_mentioned"
        self.documents_read = 0
        self.comparisons = 0


def _carry_provenance(observation: dict, found: Mapping[str, Any]) -> dict:
    """Copy how a reading was produced onto the observation that publishes it.

    `claim_analysis_deterministic` stamps every rule reading with
    `extraction_method`/`rule_version`, and until 2026-08-26 nothing carried
    them into the contract -- the driver built its observation field by field
    and simply did not name them, so a value read by a regex was
    indistinguishable from one a model produced. That is the same
    written-by-several-call-sites-read-by-none shape as `raw_page_text` and
    `medical_review_adopted`.

    It matters for audit: the two have different failure modes. A rule cannot
    invent a value but can miss a variant form; a model reads any phrasing but
    was measured returning different answers on identical text across runs
    (CASE_705 vs CASE_713). A reviewer checking a suspicious value needs to
    know which one is in front of them.

    Absent means `model_read`, so nothing is stamped on an ordinary reading.
    """
    method = found.get("extraction_method")
    if method:
        observation["extraction_method"] = method
        if found.get("rule_version"):
            observation["rule_version"] = found["rule_version"]
    return observation


def _partial_observation(found: Mapping[str, Any], *, observation_id: str,
                         kind: str | None, priority_rank: int, wave: str,
                         reference: Mapping[str, Any]) -> dict:
    """A cited reading that failed the trusted-value bar, recorded not discarded.

    See `_consume` for why this exists: dropping such a reading left the field
    on its `unavailable`/`sources_exhausted` default, so the contract reported
    that no source mentioned a value the model had quoted verbatim.

    It is never canonical. `_settle_unresolved` keeps the field `unavailable`
    and `selected_observation_ids` empty; the schema refuses a partial reading
    that appears in `selected_observation_ids`.
    """
    observation = {
        "observation_id": observation_id,
        "value_state": "asserted",
        "value": found["value"],
        "partial_reading": True,
        "complete": bool(found.get("complete", True)),
        "unambiguous": bool(found.get("unambiguous", True)),
        "source_priority_rank": priority_rank,
        "extraction_wave": wave,
        "evidence_references": [reference],
    }
    if kind is not None:
        observation["source_document_kind"] = kind
    return _carry_provenance(observation, found)


def _settle_unresolved(outcome: "FieldExtractionOutcome") -> None:
    """Report a field that ended with no trusted value, distinguishing WHY.

    Three endings, and a reviewer is owed a different thing by each:

    * `explicitly_absent` -- a source stated the thing is not present.
    * `partial_value_only` -- a source cited a value that failed the
      trusted-value requirements. The records are NOT silent, and the quote is
      preserved, so the reviewer should read the source rather than request
      more documents.
    * otherwise the constructor default stands: nothing was established.
    """
    if outcome.status != "unavailable" or not outcome.observations:
        return
    partial = [o for o in outcome.observations if o.get("partial_reading")]
    absent = [o for o in outcome.observations
              if o.get("value_state") == "explicitly_absent"]
    if absent and not partial:
        # A stated absence, and nothing cited a value against it.
        outcome.status = "explicitly_absent"
        outcome.stop_reason = "explicitly_absent"
        outcome.selected_ids = []
        outcome.reason = "라우팅된 출처가 이 항목이 없다고 기재하고 있습니다"
        return
    if not partial:
        return
    # A CITED VALUE OUTRANKS A STATED ABSENCE. One source saying the thing is
    # not present does not undo another source quoting it: on CASE_9417's
    # `objective_change` the field held three readings -- `요통 호전 경향`,
    # `처음보다는 40% 호전된 상태`, and an imaging line `No definite abnormal
    # signal change in bones` -- and reporting the whole field as
    # `explicitly_absent` told a reviewer the records were silent about a
    # change the records had described twice. The absence readings stay in
    # `observations`; they simply no longer decide the field.
    outcome.stop_reason = "partial_value_only"
    outcome.unavailable_reason = "partial_reading_only"
    outcome.selected_ids = []
    incomplete = sum(1 for o in partial if not o.get("complete", True))
    ambiguous = sum(1 for o in partial if not o.get("unambiguous", True))
    parts = []
    if incomplete:
        parts.append(f"불완전 {incomplete}건")
    if ambiguous:
        parts.append(f"다의적 {ambiguous}건")
    outcome.reason = (
        f"우선순위 출처에서 값을 확인했으나 신뢰값 요건을 충족하지 못했습니다"
        f"({', '.join(parts)}). 인용은 보존되어 있으므로 검토자가 원문을 "
        "확인해야 합니다")


def _values_disagree(left: Any, right: Any) -> bool:
    """Whether two extracted values are a real contradiction.

    A missing mention is never a conflict (`missing_mention_is_conflict:
    false`): only two *asserted* values that differ are. String comparison is
    whitespace-normalized so a line-wrap is not mistaken for a disagreement.
    """
    def normal(value: Any) -> Any:
        if isinstance(value, str):
            return " ".join(value.split())
        if isinstance(value, list):
            return tuple(normal(item) for item in value)
        return value

    return normal(left) != normal(right)


def resolve_field(
    plan: selection.FieldPlan,
    field_row: Mapping[str, Any],
    config: Mapping[str, Any],
    extract,
    page_text: Mapping[tuple[str, int], str],
    observation_ids,
) -> FieldExtractionOutcome:
    """Walk one field's priority ladder and apply the stop rule.

    `extract(field_row, document_id, kind, rank)` returns either None (this
    document does not state the field) or a dict with `value`, `page`, `quote`.
    It is injected so the ordering logic is testable without a provider.
    """
    outcome = FieldExtractionOutcome(
        field_id=plan.field_id,
        domain_code=plan.domain_code,
        grade=selection.field_grade(field_row),
    )
    if plan.skip_reason is not None:
        # A field with nothing to read is UNAVAILABLE, not not_applicable.
        # `not_applicable` says the case's own facts exclude the field, and it
        # owes a quote proving that; "this pack contains no document of the
        # kind this field routes to" proves nothing about the case -- it is a
        # gap in the records. Filing it as not_applicable would convert a
        # missing document into a decided finding.
        outcome.status = "unavailable"
        outcome.stop_reason = "sources_exhausted"
        outcome.unavailable_reason = "source_document_missing"
        outcome.reason = plan.skip_reason
        return outcome

    budget = selection.comparison_budget(field_row, config)
    trusted: dict | None = None

    for step in plan.steps:
        for document_id, kind in zip(step.document_ids, step.document_kinds):
            if trusted is not None and outcome.comparisons >= budget:
                break
            found = extract(field_row, document_id, kind, step.priority_rank)
            outcome.documents_read += 1
            if found is None:
                continue
            text = page_text.get((document_id, found["page"]))
            if text is None:
                continue
            reference = build_reference(
                document_id, found["page"], found["quote"], text)
            if reference is None:
                # An unverifiable or ambiguous quote is dropped, never repaired.
                continue
            if not selection.is_trusted_value(
                from_priority_source=True,
                has_exact_quote=True,
                complete=bool(found.get("complete", True)),
                unambiguous=bool(found.get("unambiguous", True)),
            ):
                # Recorded, never canonical -- see `_partial_observation`.
                outcome.observations.append(_partial_observation(
                    found, observation_id=next(observation_ids), kind=kind,
                    priority_rank=step.priority_rank, wave=plan.wave,
                    reference=reference))
                continue
            observation = {
                "observation_id": next(observation_ids),
                "value_state": "asserted",
                "value": found["value"],
                "source_document_kind": kind,
                "source_priority_rank": step.priority_rank,
                "extraction_wave": plan.wave,
                "evidence_references": [reference],
            }
            _carry_provenance(observation, found)
            outcome.observations.append(observation)
            if trusted is None:
                trusted = observation
                if budget == 0:
                    break
            else:
                outcome.comparisons += 1
                if _values_disagree(trusted["value"], observation["value"]):
                    # Both readings are preserved and NEITHER becomes canonical.
                    outcome.status = "conflict"
                    outcome.stop_reason = "conflict_found"
                    outcome.selected_ids = []
                    outcome.reason = (
                        "두 개의 독립된 우선순위 출처가 이 항목에 서로 다른 값을 기재하고 있습니다. 두 기재를 모두 보존해 consistency_check에서 확인합니다"
                    )
                    return outcome
                break
        if trusted is not None and outcome.comparisons >= budget:
            break

    if trusted is not None:
        outcome.status = "asserted"
        outcome.stop_reason = "trusted_value_found"
        outcome.selected_ids = [trusted["observation_id"]]
        outcome.reason = "확보 가능한 최상위 우선순위 출처에서 신뢰할 수 있는 값을 확인했습니다"
    elif outcome.documents_read:
        # Documents WERE opened and none stated the field. Kept distinct from
        # the constructor default because the two ask a reviewer for different
        # things: this says the records in hand are silent, so further records
        # are needed; the default says the ladder reached nothing at all. On
        # CASE_053 every unavailable field printed the same sentence, so
        # 사고일 (unreachable -- the fact sits in an insurer document no route
        # reads) was indistinguishable from 현재 치료 상태 (genuinely absent --
        # the case holds no 최종진료기록).
        outcome.reason = (
            f"이 항목의 우선순위 출처 {outcome.documents_read}건을 읽었으나 "
            "어느 자료에도 기재가 없었습니다"
        )
        # A partial reading overrides that sentence: the sources were not
        # silent, they were merely not trustworthy enough to be canonical.
        _settle_unresolved(outcome)
    return outcome


class DocumentCache:
    """Every provider read of a document, kept for the rest of the run.

    The budget is one provider call per document per RUN, not per wave. Wave A
    and wave B plans are computed together and inverted to (document -> fields),
    so opening a document extracts every field it is on the ladder for at once;
    wave B and the critical-field comparison then read this cache. A wave
    boundary orders the stop rule's priority -- it does not buy a second read.

    `calls` is what the trace publishes, so "at most once" is a recorded number
    rather than a claim about the code.
    """

    def __init__(self, extract) -> None:
        self._extract = extract
        self._results: dict[str, dict[str, Any]] = {}
        self.calls: dict[str, int] = {}
        self.order: list[str] = []

    def read(
        self,
        document_id: str,
        kind: str | None,
        field_rows: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Structured result for this document, reading it at most once."""
        if document_id in self._results:
            return self._results[document_id]
        found = self._extract(document_id, kind, list(field_rows)) or {}
        self._results[document_id] = found
        self.calls[document_id] = self.calls.get(document_id, 0) + 1
        self.order.append(document_id)
        return found

    def cached(self, document_id: str) -> dict[str, Any] | None:
        return self._results.get(document_id)

    def was_read(self, document_id: str) -> bool:
        return document_id in self._results


def industrial_trigger_active(
    outcomes: Mapping[str, "FieldExtractionOutcome"],
    assessments: Sequence[Mapping[str, Any]] = (),
) -> bool:
    """Whether the industrial-accident filing route may open anything.

    Two independent triggers, per the routing config's
    `industrial_accident_filed_or_applicable`: an explicit work-activity fact
    already extracted, or the industrial_accident case type coming back
    applicable. Absent both, the route contributes nothing and costs no read --
    which is the point of gating it rather than always running it.

    The accepted values come from `case_types.WORK_SUPPORTING`, the same set
    the case-type verdict uses, and are deliberately not restated here. They
    had drifted: the verdict accepted `work_activity` and `on_duty` while this
    gate accepted only the first, so a case recorded as `on_duty` could be
    ruled 산재 `applicable` by one rule and denied the administrative route by
    the other. Two independent copies of one policy is how that happens; one
    constant is why it cannot happen again.
    """
    for assessment in assessments:
        if (assessment.get("case_type") == "industrial_accident"
                and assessment.get("status") == "applicable"):
            return True
    outcome = outcomes.get("work_activity_context")
    if outcome is not None and outcome.status == "asserted":
        for observation in outcome.observations:
            if observation.get("value") in case_types_mod.WORK_SUPPORTING:
                return True
    return False


def resolve_from_cache(
    plan: selection.FieldPlan,
    field_row: Mapping[str, Any],
    config: Mapping[str, Any],
    cache: DocumentCache,
    page_text: Mapping[tuple[str, int], str],
    observation_ids,
) -> FieldExtractionOutcome:
    """Walk one field's priority ladder against the document cache.

    Identical stop rule to before -- first trusted value wins, a critical field
    buys one extra independent source -- but every read here is a cache lookup:
    the documents were opened once, up front, for every field they can answer.
    """
    outcome = FieldExtractionOutcome(
        field_id=plan.field_id,
        domain_code=plan.domain_code,
        grade=selection.field_grade(field_row),
    )
    if plan.skip_reason is not None:
        outcome.status = "unavailable"
        outcome.stop_reason = "sources_exhausted"
        outcome.unavailable_reason = "source_document_missing"
        outcome.reason = plan.skip_reason
        return outcome

    budget = selection.comparison_budget(field_row, config)
    trusted: dict | None = None

    for step in plan.steps:
        for document_id, kind in zip(step.document_ids, step.document_kinds):
            if trusted is not None and outcome.comparisons >= budget:
                break
            document = cache.cached(document_id)
            if document is None:
                continue
            found = document.get(plan.field_id)
            outcome.documents_read += 1
            if not found:
                continue
            text = page_text.get((document_id, found.get("page")))
            if text is None:
                continue
            reference = build_reference(
                document_id, found["page"], found["quote"], text)
            if reference is None:
                # An unverifiable or ambiguous quote is dropped, never repaired.
                continue
            if not selection.is_trusted_value(
                from_priority_source=True,
                has_exact_quote=True,
                complete=bool(found.get("complete", True)),
                unambiguous=bool(found.get("unambiguous", True)),
            ):
                # Recorded, never canonical -- see `_partial_observation`.
                outcome.observations.append(_partial_observation(
                    found, observation_id=next(observation_ids), kind=kind,
                    priority_rank=step.priority_rank, wave=plan.wave,
                    reference=reference))
                continue
            observation = {
                "observation_id": next(observation_ids),
                "value_state": "asserted",
                "value": found["value"],
                "source_document_kind": kind,
                # The rung of THIS field's route, not the read order.
                "source_priority_rank": step.priority_rank,
                "extraction_wave": plan.wave,
                "evidence_references": [reference],
            }
            _carry_provenance(observation, found)
            outcome.observations.append(observation)
            if trusted is None:
                trusted = observation
                if budget == 0:
                    break
            else:
                outcome.comparisons += 1
                if _values_disagree(trusted["value"], observation["value"]):
                    outcome.status = "conflict"
                    outcome.stop_reason = "conflict_found"
                    outcome.selected_ids = []
                    outcome.reason = (
                        "두 개의 독립된 우선순위 출처가 이 항목에 서로 다른 값을 기재하고 있습니다. 두 기재를 모두 보존해 consistency_check에서 확인합니다"
                    )
                    return outcome
                break
        if trusted is not None and outcome.comparisons >= budget:
            break

    if trusted is not None:
        outcome.status = "asserted"
        outcome.stop_reason = "trusted_value_found"
        outcome.selected_ids = [trusted["observation_id"]]
        outcome.reason = "확보 가능한 최상위 우선순위 출처에서 신뢰할 수 있는 값을 확인했습니다"
    elif outcome.documents_read:
        # Documents WERE opened and none stated the field. Kept distinct from
        # the constructor default because the two ask a reviewer for different
        # things: this says the records in hand are silent, so further records
        # are needed; the default says the ladder reached nothing at all. On
        # CASE_053 every unavailable field printed the same sentence, so
        # 사고일 (unreachable -- the fact sits in an insurer document no route
        # reads) was indistinguishable from 현재 치료 상태 (genuinely absent --
        # the case holds no 최종진료기록).
        outcome.reason = (
            f"이 항목의 우선순위 출처 {outcome.documents_read}건을 읽었으나 "
            "어느 자료에도 기재가 없었습니다"
        )
        # A partial reading overrides that sentence: the sources were not
        # silent, they were merely not trustworthy enough to be canonical.
        _settle_unresolved(outcome)
    return outcome


def resolve_opportunistic(
    plan: selection.FieldPlan,
    field_row: Mapping[str, Any],
    cache: DocumentCache,
    page_text: Mapping[tuple[str, int], str],
    observation_ids,
) -> FieldExtractionOutcome:
    """An opportunistic field: ride along, never pay for a read.

    Two constraints, both from the routing config. The value may only come from
    a document ALREADY read, and only from one on this field's own route -- an
    open document that the field does not route to is not a source for it. The
    recorded rank is the route's, so a ride-along is never mistaken for a
    higher-priority reading.
    """
    outcome = FieldExtractionOutcome(
        field_id=plan.field_id,
        domain_code=plan.domain_code,
        grade=selection.field_grade(field_row),
    )
    # `not_scheduled`, not `not_mentioned`: an opportunistic field never buys a
    # read of its own, so "no source discussed it" would overstate what was
    # looked at. The distinction is what tells a reviewer this is a scheduling
    # decision rather than a records gap they could close.
    outcome.unavailable_reason = "not_scheduled"
    outcome.reason = (
        "이 항목의 경로에서 이미 열려 있던 문서에 기재가 없었고, 기회적 항목은 자체적으로 새 문서를 열지 않습니다"
    )
    for step in plan.steps:
        for document_id, kind in zip(step.document_ids, step.document_kinds):
            if not cache.was_read(document_id):
                continue
            found = (cache.cached(document_id) or {}).get(plan.field_id)
            if not found:
                continue
            # A cached record is not always an asserted value. The main pass
            # (`_absorb`) reads `presence` first and routes an
            # `explicitly_absent` record to an observation carrying a reason
            # and NO value -- a source stating the field is absent is evidence,
            # not a reading. This path never checked, so it reached
            # `found["value"]` on exactly that record and took the whole driver
            # down with a KeyError: CASE_712 (2026-08-21) crashed stage 5 after
            # 235.9s, on a case whose only defect was that one ride-along
            # source said a field was not present.
            #
            # A ride-along is opportunistic by definition: it answers from a
            # document another field already opened, so anything it cannot
            # cleanly read is skipped rather than repaired. The absence is
            # still recorded by the owning field's own read.
            if found.get("presence", "asserted") != "asserted":
                continue
            if found.get("value") is None or not found.get("quote"):
                continue
            text = page_text.get((document_id, found.get("page")))
            if text is None:
                continue
            reference = build_reference(
                document_id, found["page"], found["quote"], text)
            if reference is None:
                continue
            observation = {
                "observation_id": next(observation_ids),
                "value_state": "asserted",
                "value": found["value"],
                "source_document_kind": kind,
                "source_priority_rank": step.priority_rank,
                "extraction_wave": "opportunistic",
                "evidence_references": [reference],
            }
            _carry_provenance(observation, found)
            outcome.observations.append(observation)
            outcome.status = "asserted"
            outcome.stop_reason = "trusted_value_found"
            outcome.selected_ids = [observation["observation_id"]]
            outcome.reason = "이 항목의 경로에서 이미 열려 있던 문서에서 함께 확인했습니다"
            return outcome
    return outcome


class FieldProgress:
    """One field's walk down its own priority ladder.

    Holds the position (which rung, which document within it) so the scheduler
    can ask "what do you need next?" without re-deriving the ladder, and holds
    the partial result so a field that has found one trusted value but still
    owes a comparison keeps its place.
    """

    __slots__ = ("plan", "field_row", "budget", "step_index", "doc_index",
                 "trusted", "outcome", "done", "rank_by_document")

    def __init__(self, plan, field_row, budget: int, outcome) -> None:
        self.plan = plan
        self.field_row = field_row
        self.budget = budget
        self.step_index = 0
        self.doc_index = 0
        self.trusted: dict | None = None
        self.outcome = outcome
        self.done = False
        # Every document anywhere on this field's ladder, with the rung it sits
        # on. Used to answer "may this field legitimately be extracted from
        # this document?" for a document being opened NOW on someone else's
        # behalf -- which is a different question from "is this the document
        # the field wants next?".
        self.rank_by_document: dict[str, int] = {}
        for step in plan.steps:
            for document_id in step.document_ids:
                self.rank_by_document.setdefault(
                    document_id, step.priority_rank)

    def routes_to(self, document_id: str) -> bool:
        """Whether this field may ever be read from `document_id`.

        A field is only ever extracted from a document its own route ranks --
        an open document it does not route to is not a source for it.
        """
        return document_id in self.rank_by_document

    def next_document(self) -> tuple[str, str] | None:
        """The next (document_id, kind) this field still wants to read.

        None means the field is finished -- either it stopped, or its ladder
        ran out. A finished field never contributes a document to the schedule,
        which is what keeps a later-priority source from being opened for a
        field that already stopped.
        """
        if self.done:
            return None
        while self.step_index < len(self.plan.steps):
            step = self.plan.steps[self.step_index]
            if self.doc_index < len(step.document_ids):
                return (step.document_ids[self.doc_index],
                        step.document_kinds[self.doc_index])
            self.step_index += 1
            self.doc_index = 0
        return None

    def advance(self) -> None:
        self.doc_index += 1

    def current_rank(self) -> int:
        return self.plan.steps[self.step_index].priority_rank

    def stop(self) -> None:
        self.done = True


def _finish(progress: FieldProgress) -> None:
    """Settle a field whose walk has ended."""
    outcome = progress.outcome
    if progress.trusted is not None:
        outcome.status = "asserted"
        outcome.stop_reason = "trusted_value_found"
        outcome.selected_ids = [progress.trusted["observation_id"]]
        outcome.reason = (
            "확보 가능한 최상위 우선순위 출처에서 신뢰할 수 있는 값을 확인했습니다")
    else:
        # Only explicitly-absent or partial readings survived. Which of the two
        # it was decides what the contract says and what a reviewer should do.
        _settle_unresolved(outcome)
    progress.done = True


def _consume(
    progress: FieldProgress,
    document_id: str,
    kind: str,
    found: Mapping[str, Any] | None,
    page_text: Mapping[tuple[str, int], str],
    observation_ids,
) -> None:
    """Apply one document's answer for one field, and apply the stop rule.

    This is the whole of the selective decision: a field that gets a trusted
    value stops here, and stopping is what removes its remaining documents from
    the schedule. A critical field spends its one comparison and then stops
    too, whether or not the second source agreed.
    """
    outcome = progress.outcome
    outcome.documents_read += 1
    if not found:
        return

    presence = found.get("presence", "asserted")
    page = found.get("page")
    text = page_text.get((document_id, page))
    if text is None:
        return
    reference = build_reference(document_id, page, found.get("quote", ""), text)
    if reference is None:
        # An unverifiable or ambiguous quote is dropped, never repaired.
        return

    if presence == "explicitly_absent":
        # A stated absence is evidence, and it is recorded with the sentence
        # that states it. It does not become a value, and it does not satisfy
        # the search -- a later source may still assert one.
        outcome.observations.append({
            "observation_id": next(observation_ids),
            "value_state": "explicitly_absent",
            "reason": found.get("reason") or "해당 출처가 이 항목이 없다고 기재하고 있습니다",
            "source_document_kind": kind,
            "source_priority_rank": progress.current_rank(),
            "extraction_wave": progress.plan.wave,
            "evidence_references": [reference],
        })
        return

    if not selection.is_trusted_value(
        from_priority_source=True,
        has_exact_quote=True,
        complete=bool(found.get("complete", True)),
        unambiguous=bool(found.get("unambiguous", True)),
    ):
        # A cited reading that is merely PARTIAL or AMBIGUOUS is recorded, and
        # deliberately does not become `trusted`: the ladder keeps walking, so
        # a complete lower-priority source can still win. Recording it changes
        # only what the contract says when nothing better is ever found.
        #
        # Discarding it outright is what this replaces, and the failure was not
        # cosmetic. `_finish` settles a field from `outcome.observations`, so a
        # dropped reading left the field on its constructor default --
        # `unavailable` / `sources_exhausted` / "어떤 출처도 이 항목을 기재하지
        # 않았습니다". The contract then asserted that no source mentioned the
        # field, about text the model had quoted verbatim. Measured on
        # CASE_7015 (2026-08-26): 5 of 40 `unavailable` fields were this case,
        # including 사고일 ('3/26'), 수술명 ('CRIF with K-wires, distal radius,
        # Lt'), 장해 영구성 ('본 장해는 영구 장해임') and 맥브라이드식 기준 --
        # each dropped for `complete: false`, which the model set honestly
        # because a second procedure or a fuller date sat beside the value it
        # returned. An evaluation comparing this contract against ground truth
        # cannot tell such a field from one the records genuinely never state.
        #
        # This mirrors `explicitly_absent` above: evidence is preserved, the
        # search is not satisfied. The four trusted-value requirements in
        # `extraction_policy.trusted_value_requirements` are untouched -- this
        # value still fails them, and still never becomes canonical.
        outcome.observations.append(_partial_observation(
            found, observation_id=next(observation_ids), kind=kind,
            priority_rank=progress.current_rank(),
            wave=progress.plan.wave, reference=reference))
        return

    observation = {
        "observation_id": next(observation_ids),
        "value_state": "asserted",
        "value": found["value"],
        "source_document_kind": kind,
        # The rung of THIS field's route, not the read order.
        "source_priority_rank": progress.current_rank(),
        "extraction_wave": progress.plan.wave,
        "evidence_references": [reference],
    }
    _carry_provenance(observation, found)
    outcome.observations.append(observation)

    if progress.trusted is None:
        progress.trusted = observation
        if progress.budget == 0:
            # Ordinary field: the first trusted value ends the search, and
            # every lower-priority document it would have opened is now never
            # scheduled.
            _finish(progress)
        return

    outcome.comparisons += 1
    if _values_disagree(progress.trusted["value"], observation["value"]):
        outcome.status = "conflict"
        outcome.stop_reason = "conflict_found"
        outcome.selected_ids = []
        outcome.reason = (
            "두 개의 독립된 우선순위 출처가 이 항목에 서로 다른 값을 기재하고 있습니다. 두 기재를 모두 보존해 consistency_check에서 확인합니다")
        progress.done = True
        return
    if outcome.comparisons >= progress.budget:
        _finish(progress)


def extract_all(
    *,
    config: Mapping[str, Any],
    documents: Sequence[selection.DocumentRef],
    extract,
    page_text: Mapping[tuple[str, int], str],
    observation_ids,
    non_medical_reader=None,
) -> tuple[list[FieldExtractionOutcome], DocumentCache]:
    """Read only what an unresolved field actually asks for.

    The loop is demand-driven, and that is the point of the whole design. Each
    round asks every *still-open* field which document it wants next, groups
    the answers so one provider call serves every field asking for the same
    document, reads it once, and lets the stop rule settle whatever it can.
    Fields that stop drop out, so their lower-priority sources are never
    scheduled -- a case whose 진단서 answers everything never opens the
    입퇴원요약 at all.

    Three properties hold simultaneously, and none is incidental:

    * **Priority order.** Rounds are ordered by the rung the asking fields sit
      on, so a rank-1 source is always read before a rank-2 one.
    * **One call per document per RUN.** The cache is consulted first, so a
      document wanted by an A field and later by a B field is read once and
      served from memory the second time.
    * **Batching.** Fields asking for the same document in the same round share
      one call, so reading a 진단서 extracts everything it can answer at once.
    """
    plans = (selection.plan_wave(config, documents, "A")
             + selection.plan_wave(config, documents, "B"))
    conditional = {
        row["field_id"] for row in config.get("fields") or []
        if selection.route_activation_condition(row, config) != "always"
    }
    by_field_row = {row["field_id"]: row for row in config.get("fields") or []}
    kind_by_document = {ref.document_id: ref.kind for ref in documents}
    cache = DocumentCache(extract)
    opportunistic = [
        (field_row, selection.plan_field(field_row, config, documents))
        for field_row in selection.opportunistic_fields(config)
    ]

    progress: list[FieldProgress] = []
    outcomes: list[FieldExtractionOutcome] = []
    by_field: dict[str, FieldExtractionOutcome] = {}
    for plan in plans:
        if plan.field_id in conditional:
            continue
        field_row = by_field_row[plan.field_id]
        outcome = FieldExtractionOutcome(
            field_id=plan.field_id,
            domain_code=plan.domain_code,
            grade=selection.field_grade(field_row),
        )
        outcomes.append(outcome)
        by_field[plan.field_id] = outcome
        if plan.skip_reason is not None:
            # Nothing to read is UNAVAILABLE, not not_applicable: a pack that
            # holds no document of this kind proves nothing about the case.
            outcome.stop_reason = "sources_exhausted"
            outcome.unavailable_reason = "source_document_missing"
            outcome.reason = plan.skip_reason
            continue
        progress.append(FieldProgress(
            plan, field_row,
            selection.comparison_budget(field_row, config), outcome))

    while True:
        # Ask only the open fields. A field that stopped contributes nothing,
        # which is exactly how its remaining documents stay unread.
        wanted: dict[str, list[FieldProgress]] = {}
        best_rank: dict[str, int] = {}
        for item in progress:
            nxt = item.next_document()
            if nxt is None:
                if not item.done:
                    _finish(item)
                continue
            document_id, _kind = nxt
            wanted.setdefault(document_id, []).append(item)
            rank = item.current_rank()
            if document_id not in best_rank or rank < best_rank[document_id]:
                best_rank[document_id] = rank
        if not wanted:
            break

        # Highest-priority rung first, so the stop rule still sees the most
        # authoritative source before any fallback.
        document_id = sorted(wanted, key=lambda d: (best_rank[d], d))[0]
        askers = wanted[document_id]
        kind = kind_by_document.get(document_id)

        # The document is opened once and never again, so this one call must
        # ask for everything it will EVER be asked for. `askers` are the fields
        # whose ladder points here now; `latecomers` are still-open fields that
        # rank this document further down their own ladder and would otherwise
        # arrive after the only call was already made -- finding a cache entry
        # that never contained their field and recording `unavailable` for a
        # fact the document plainly states.
        #
        # Extracting for them costs no extra provider call: they ride the call
        # that was happening anyway. What they do NOT get is early adoption --
        # their answer is held in the cache and consumed only when their own
        # ladder reaches this rung, so priority order is unaffected.
        asking_ids = {item.plan.field_id for item in askers}
        latecomers = [
            item for item in progress
            if not item.done
            and item.plan.field_id not in asking_ids
            and item.routes_to(document_id)
        ]
        # Opportunistic fields never schedule a document, but they must be
        # included in the only provider call for a document another field has
        # already paid to open. Otherwise the later cache lookup cannot tell
        # silence from a question that was never asked.
        opportunistic_rows = [
            field_row for field_row, plan in opportunistic
            if any(document_id in step.document_ids for step in plan.steps)
        ]
        field_rows = ([item.field_row for item in askers]
                      + [item.field_row for item in latecomers]
                      + opportunistic_rows)
        document = cache.read(document_id, kind, field_rows)
        for item in askers:
            _consume(item, document_id, kind,
                     document.get(item.plan.field_id), page_text,
                     observation_ids)
            item.advance()

    for item in progress:
        if not item.done:
            _finish(item)

    for field_row, plan in opportunistic:
        outcome = resolve_opportunistic(
            plan, field_row, cache, page_text, observation_ids)
        outcomes.append(outcome)
        by_field[field_row["field_id"]] = outcome

    # Conditional routes last: their trigger is a fact the pass above produced.
    if conditional:
        active = industrial_trigger_active(by_field)
        for field_id in sorted(conditional):
            field_row = by_field_row[field_id]
            outcome = FieldExtractionOutcome(
                field_id=field_id,
                domain_code=field_row["domain_code"],
                grade=selection.field_grade(field_row),
            )
            if not active:
                outcome.status = "unavailable"
                outcome.stop_reason = "sources_exhausted"
                # The route never opened, so no source was consulted and none
                # is missing. `not_mentioned` claimed documents had been read
                # and were silent, which would send a reviewer requesting
                # records that could not have helped.
                outcome.unavailable_reason = "route_not_activated"
                outcome.reason = FILING_ROUTE_NOT_TRIGGERED_REASON
                outcomes.append(outcome)
                continue
            found = non_medical_reader(field_row) if non_medical_reader else None
            if not found:
                # The trigger fired but there is no administrative source to
                # read -- and while none is produced anywhere, this is the only
                # branch reachable. `outside_poc_scope` rather than
                # `source_document_missing`: the case is not missing a
                # document, the pipeline is missing a producer, and recording
                # it as a records gap would send someone looking for a file
                # nobody was ever going to write.
                outcome.status = "unavailable"
                outcome.stop_reason = "sources_exhausted"
                outcome.unavailable_reason = "outside_poc_scope"
                outcome.reason = FILING_ROUTE_DEFERRED_REASON
                outcomes.append(outcome)
                continue
            observation = {
                "observation_id": next(observation_ids),
                "value_state": "asserted",
                "value": found["value"],
                "source_document_kind": found.get(
                    "source_document_kind", "other_medical"),
                "source_priority_rank": 1,
                "extraction_wave": "B",
                "evidence_references": [found["evidence_reference"]],
            }
            outcome.status = "asserted"
            outcome.stop_reason = "trusted_value_found"
            outcome.observations = [observation]
            outcome.selected_ids = [observation["observation_id"]]
            outcome.reason = "산재 접수 관련 출처에 기재되어 있습니다"
            outcomes.append(outcome)

    return outcomes, cache


def extract_additional(
    *,
    config: Mapping[str, Any],
    documents: Sequence[selection.DocumentRef],
    assessments: Sequence[Mapping[str, Any]],
    outcomes_by_field: Mapping[str, FieldExtractionOutcome],
    extract,
    page_text: MutableMapping[tuple[str, int], str],
    observation_ids,
) -> tuple[list[FieldExtractionOutcome], int, list[Mapping[str, Any]]]:
    """Stage 3-a: re-open unavailable fields from non-medical sources.

    Runs AFTER case-type assessment, because which sources are worth opening
    depends on which types are in play. Only fields the common pass left
    `unavailable` are re-read, so a 진료기록's reading is never overwritten and
    a case whose medical records answered everything pays nothing here.

    Returns REPLACEMENT outcomes for the fields it settled; a field it could
    not settle keeps the common pass's outcome untouched.

    The PLAN is returned as a third element, not because this function needs
    it, but because the trace's `documents` array is built BEFORE this round
    runs and therefore cannot see the documents it opened. Without the plan,
    every 법률의견서 stage 3-a reads is labelled `skipped / not_read / "no
    medical classification, so no route reaches it"` while the same run's
    result file carries its verbatim quotes -- observed on CASE_713, where all
    three of DOC_006/007/008 were recorded as unread. Recomputing the plan at
    the trace site is not an option: `read_plan` keys off the PRE-3-a outcome
    statuses, which the caller has already replaced by then. See the caller.
    """
    plan = additional_mod.read_plan(
        config, assessments, outcomes_by_field, documents)
    if not plan:
        return [], 0, []

    by_field_row = {row["field_id"]: row for row in config.get("fields") or []}
    replacements: dict[str, FieldExtractionOutcome] = {}

    for entry in plan:
        field_rows = [by_field_row[field_id] for field_id in entry["field_ids"]
                      if field_id in by_field_row]
        if not field_rows:
            continue
        # `kind=None`: these documents have no medical form kind, and passing a
        # medical one would misdescribe the document to the model. The coarse
        # type names it instead.
        readings = extract(entry["document_id"], None, field_rows,
                           entry["source_type"]) or {}
        for field_row in field_rows:
            field_id = field_row["field_id"]
            payload = readings.get(field_id) or {}
            if payload.get("presence") != "asserted":
                continue
            page = payload.get("page")
            quote = payload.get("quote")
            if not isinstance(page, int) or not isinstance(quote, str):
                continue
            reference = build_reference(
                entry["document_id"], page, quote,
                page_text.get((entry["document_id"], page), ""))
            if reference is None:
                # Same rule as the common pass: a quote the served text does
                # not contain verbatim is not evidence, whatever the document.
                continue
            outcome = replacements.get(field_id)
            if outcome is None:
                outcome = FieldExtractionOutcome(
                    field_id=field_id,
                    domain_code=field_row["domain_code"],
                    grade=selection.field_grade(field_row),
                )
                replacements[field_id] = outcome
            observation = {
                "observation_id": next(observation_ids),
                "value_state": "asserted",
                "value": payload.get("value"),
                # The COARSE type, never a medical kind -- see the module
                # docstring in claim_analysis_additional.
                "source_document_type": entry["source_type"],
                "source_priority_rank": entry["field_ranks"][field_id],
                "extraction_wave": "type_conditional",
                "evidence_references": [reference],
            }
            _carry_provenance(observation, payload)
            outcome.observations.append(observation)
            outcome.documents_read += 1

    settled: list[FieldExtractionOutcome] = []
    for field_id, outcome in replacements.items():
        values = [observation["value"] for observation in outcome.observations]
        disagree = any(_values_disagree(values[0], other)
                       for other in values[1:])
        if disagree:
            # Both readings kept, NEITHER elected -- the schema requires
            # `selected_observation_ids` to be empty here, and the rule is the
            # point rather than a formality: electing one would make this stage
            # pick a winner between two 법률의견서 it has no standing to judge.
            # Every other settlement site in this module already does one or
            # the other; assigning the whole list before the branch (the shape
            # this replaced) violated BOTH arms at once and was invisible until
            # a field collected two observations for the first time.
            outcome.selected_ids = []
            outcome.status = "conflict"
            outcome.stop_reason = "conflict_found"
            outcome.reason = "비의료 출처 간 기재가 다릅니다"
        else:
            # Agreement: elect the highest-priority reading. The observations
            # are appended in plan order, which is priority order, so the first
            # is the one a 법률의견서 gave over an insurer letter's summary.
            outcome.selected_ids = [outcome.observations[0]["observation_id"]]
            outcome.status = "asserted"
            outcome.stop_reason = "trusted_value_found"
            outcome.reason = "사건유형별 추가 확인 출처에 기재되어 있습니다"
        settled.append(outcome)
    # One provider call per planned document -- the round batches exactly as
    # the common pass does, so calls equal documents opened.
    return settled, len(plan), plan


def _observation_id_sequence(start: int = 1):
    counter = start
    while True:
        yield f"CAO_{counter:04d}"
        counter += 1


def _candidate_id_sequence(start: int = 1):
    counter = start
    while True:
        yield f"CAC_{counter:04d}"
        counter += 1


def assign_conflict_candidate_ids(
    outcomes: Sequence[FieldExtractionOutcome],
) -> dict[str, list[str]]:
    """Conflict candidate ids, decided ONCE for the whole run.

    Two artifacts need these ids and they must be the same ids: the result's
    `conflict_candidates` list, and the `conflict_candidate_ids` on any policy
    requirement resting on a disputed fact. Generating them independently in
    two places produced the defect this function exists to remove -- the policy
    side was handed an empty list, so a requirement whose underlying fact was
    in dispute published `evidence_status: supported` and cited nothing.

    Ordering is deterministic by construction: `outcomes` is built in plan
    order, so CAC_0001 is always the same field for the same inputs. Nothing
    downstream may mint an id of its own -- `build_result` takes this mapping
    rather than reproducing it.
    """
    candidate_ids = _candidate_id_sequence()
    assigned: dict[str, list[str]] = {}
    for outcome in outcomes:
        if outcome.status == "conflict":
            assigned[outcome.field_id] = [next(candidate_ids)]
    return assigned


def field_result(
    outcome: FieldExtractionOutcome,
    *,
    priority_grade: str,
    authority: str,
    conflict_candidate_ids: Sequence[str] = (),
) -> dict:
    """One field's published state, under the five-state contract.

    The states are not interchangeable and the schema will not let them blur:
    `asserted` elects exactly one observation, `explicitly_absent` needs the
    quote that states the absence, `unavailable` carries a reason and no
    evidence at all, `not_applicable` needs the case fact that excludes the
    field, and `conflict` keeps both readings and elects neither.
    """
    result = {
        "field_id": outcome.field_id,
        "domain_code": outcome.domain_code,
        "priority_grade": priority_grade,
        "authority": authority,
        "resolution_status": outcome.status,
        "selected_observation_ids": list(outcome.selected_ids),
        "observations": list(outcome.observations),
        "conflict_candidate_ids": list(conflict_candidate_ids),
        "stop_reason": outcome.stop_reason,
    }
    if outcome.status != "asserted":
        result["resolution_reason"] = outcome.reason
    if outcome.status == "unavailable":
        result["unavailable_reason"] = outcome.unavailable_reason
        if outcome.stop_reason == "partial_value_only":
            # A partial reading is the one `unavailable` case where something
            # WAS cited, so its quote must survive: the whole point of the
            # state is to hand a reviewer the text to check. Rewriting it to a
            # citation-free placeholder here would have undone the fix one
            # layer below the contract -- the driver would record the quote and
            # then publish a field claiming nothing was found.
            #
            # It is still not canonical: `selected_observation_ids` is empty
            # and the schema refuses a partial reading appearing in it.
            pass
        else:
            # Nothing was established, so nothing may be cited. Any reading
            # picked up along the way is rewritten to carry the same verdict --
            # an asserted value cannot survive underneath an "unavailable"
            # field that reports its sources as silent.
            result["observations"] = [{
                "observation_id": observation["observation_id"],
                "value_state": "unavailable",
                "reason": outcome.reason,
                "unavailable_reason": outcome.unavailable_reason,
                "extraction_wave": observation["extraction_wave"],
                "evidence_references": [],
            } for observation in outcome.observations]
    return result


def authority_for(domain_code: str) -> str:
    """Where a field's content came from -- never a projection claim.

    A medical-domain value here was read from a document in this case, not
    projected from the canonical medical revision. Labelling it a projection
    would describe work this lane does not do.
    """
    return (
        "claim_analysis_native" if domain_code in NATIVE_DOMAINS
        else "source_document_extraction"
    )


def priority_grade_for(field_row: Mapping[str, Any]) -> str:
    """The published grade, clamped to the schema's A/B axis.

    A `C` field only reaches extraction through a named operational override,
    and it rides the B wave when it does; publishing it as `B` states that
    honestly rather than inventing a `C` the downstream contract has no slot
    for.
    """
    grade = selection.field_grade(field_row)
    return "A" if grade == "A" else "B"


# ------------------------------------------------------------- assembly --

def build_result(
    *,
    case_id: str,
    run_id: str,
    outcomes: Sequence[FieldExtractionOutcome],
    config: Mapping[str, Any],
    documents: Sequence[selection.DocumentRef],
    medical_revision_context: Mapping[str, Any] | None = None,
    policy_links: Sequence[Mapping[str, Any]] = (),
    filing_status_by_type: Mapping[str, str] | None = None,
    conflict_candidate_ids_by_field: Mapping[str, Sequence[str]] | None = None,
    model_name: str = "deterministic",
    prompt_version: str = VERSION,
) -> dict:
    by_field = {row["field_id"]: row for row in config.get("fields") or []}
    # Single ownership: the ids were decided by `assign_conflict_candidate_ids`
    # before policy linking, so both artifacts cite the same ones. The fallback
    # is for callers that assemble a result with no policy layer at all; it
    # runs the same deterministic assignment, never a second numbering
    # alongside one already handed out.
    assigned = dict(conflict_candidate_ids_by_field
                    if conflict_candidate_ids_by_field is not None
                    else assign_conflict_candidate_ids(outcomes))

    claim_facts: list[dict] = []
    conflict_candidates: list[dict] = []
    for outcome in outcomes:
        field_row = by_field[outcome.field_id]
        attached: list[str] = []
        if outcome.status == "conflict":
            candidate_id = (list(assigned.get(outcome.field_id) or [None]) or [None])[0]
            if candidate_id is None:
                raise RuntimeError(
                    f"no conflict candidate id was assigned for conflicting "
                    f"field {outcome.field_id!r}; ids are assigned once per run "
                    "and reused, never minted here")
            attached.append(candidate_id)
            conflict_candidates.append({
                "conflict_candidate_id": candidate_id,
                "field_id": outcome.field_id,
                "observation_ids": [
                    observation["observation_id"]
                    for observation in outcome.observations
                ],
                "reason": outcome.reason,
                "consistency_status": "pending_consistency_check",
            })
        claim_facts.append(field_result(
            outcome,
            priority_grade=priority_grade_for(field_row),
            authority=authority_for(outcome.domain_code),
            conflict_candidate_ids=attached,
        ))

    assessments = case_types_mod.assess_case_types(
        claim_facts, filing_status_by_type=filing_status_by_type)
    checklist = selection.presence_checklist(
        config, documents, case_types_mod.selected_case_types(assessments))

    warnings: list[str] = []
    if conflict_candidates:
        warnings.append(
            f"{len(conflict_candidates)} conflict candidate(s) recorded for "
            "consistency_check to verify; no ledger entry is created here"
        )

    result = {
        "case_id": case_id,
        "run_id": run_id,
        "component": COMPONENT,
        "status": "success",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model_info": {"model_name": model_name, "prompt_version": prompt_version},
        # `confidence` is deliberately OMITTED, not set to a number. Field-level
        # trust here is the four-condition stop rule, which is a decision rather
        # than a score; publishing a synthetic contract-level confidence would
        # be a fabricated measurement (P1).
        "review_required": bool(conflict_candidates),
        "warnings": warnings,
        "source_grounded": True,
        "schema_version": "claim_analysis_result.v0.1",
        "config_version": config["config_version"],
        # Fixed for v0.1: no field-to-variable mapping exists to project
        # through, so no other value would be true.
        "medical_projection_status": "not_configured",
        "claim_facts": claim_facts,
        "case_type_assessment": assessments,
        "policy_links": list(policy_links),
        "required_document_checklist": checklist,
        "conflict_candidates": conflict_candidates,
    }
    if medical_revision_context is not None:
        # Context only, and recorded solely because the caller observed it.
        # Declaring it is what makes the DAO verify the digest; omitting it
        # asserts nothing rather than asserting a revision this run never saw.
        result["medical_revision_context"] = dict(medical_revision_context)
    if result["review_required"]:
        # Only meaningful alongside review_required; the conflict candidates are
        # a 손해사정사's call to verify, which is consistency_check's input.
        result["reviewer_role"] = "손해사정사"
    return result


def _restamp_type_conditional(
    dispositions: list[dict],
    plan: Sequence[Mapping[str, Any]],
) -> None:
    """Mark the documents stage 3-a opened as read, in place.

    The disposition rows are built from the medical waves' plan, which runs
    before stage 3-a exists, so a 법률의견서 it opens keeps whatever label the
    earlier pass gave it -- `skipped / not_read` with "no medical
    classification, so no route reaches it". That is the opposite of what
    happened, and it is the trace, not the result, that a reviewer reads to
    ask what this stage cost.

    Only rows the plan names are touched, and `field_ids` is the UNION of the
    medical routing and this round's asks: a field can be routed medically and
    still be re-opened here, and dropping either side would understate one.
    """
    if not plan:
        return
    by_document = {row["document_id"]: row for row in dispositions}
    for entry in plan:
        row = by_document.get(entry["document_id"])
        if row is None:
            # A planned document with no disposition row cannot happen today
            # (both are built from the same `documents` list), but silently
            # inventing a row would hide a real desync if that ever changes.
            continue
        asked = [field_id for field_id in entry.get("field_ids") or []]
        row["disposition"] = "read"
        row["wave"] = "type_conditional"
        row["field_ids"] = sorted(set(row.get("field_ids") or []) | set(asked))
        row["reason"] = (
            f"stage 3-a opened it as a {entry['source_type']} for "
            f"{len(asked)} type-conditional field(s)")


def build_trace(
    *,
    case_id: str,
    run_id: str,
    config: Mapping[str, Any],
    outcomes: Sequence[FieldExtractionOutcome],
    document_dispositions: Sequence[Mapping[str, Any]],
    provider_calls: int,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    wall_time_seconds: float | None = None,
    type_conditional_documents: int = 0,
    type_conditional_calls: int = 0,
) -> dict:
    """Development/performance trace, kept out of the authority contract."""
    presence_only = sum(
        1 for row in document_dispositions
        if row.get("disposition") == "presence_only"
    )
    # `documents_read` is the COMMON pass's count and stays that way: the
    # schema keeps `type_conditional_documents_read` separate on purpose, so
    # that an SLA regression in 3-a (whose cost scales with case types in play)
    # cannot hide inside a number that scales with the field catalogue. Since
    # 3-a's documents are now stamped `read`, they have to be excluded here by
    # wave, or the two metrics would double-count the same open.
    read = sum(
        1 for row in document_dispositions
        if row.get("disposition") == "read"
        and row.get("wave") != "type_conditional"
    )
    return {
        "case_id": case_id,
        "run_id": run_id,
        "schema_version": "claim_analysis_trace.v0.1",
        "config_version": config["config_version"],
        "documents": list(document_dispositions),
        "field_stops": [{
            "field_id": outcome.field_id,
            "stop_reason": outcome.stop_reason,
            "documents_read": outcome.documents_read,
            "additional_comparisons": outcome.comparisons,
        } for outcome in outcomes],
        "metrics": {
            "documents_considered": len(document_dispositions),
            "documents_read": read,
            "documents_presence_only": presence_only,
            "a_stop_fields": sum(
                1 for outcome in outcomes
                if outcome.grade == "A" and outcome.status == "asserted"
            ),
            "b_fallback_fields": sum(
                1 for outcome in outcomes
                if outcome.grade != "A" and outcome.status == "asserted"
            ),
            "provider_calls": provider_calls,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "wall_time_seconds": wall_time_seconds,
            # Stage 3-a, counted separately. Its cost scales with how many case
            # types are in play -- the one part of this stage that grows with
            # the case rather than with the field catalogue -- so folding it
            # into the common pass's totals would hide an SLA regression there.
            "type_conditional_documents_read": type_conditional_documents,
            "type_conditional_provider_calls": type_conditional_calls,
        },
    }


def publish(
    *,
    case_id: str,
    run_id: str,
    held_by: str,
    result: Mapping[str, Any],
    trace: Mapping[str, Any],
) -> None:
    """Write both contracts through the DAO, result first.

    The result carries the exact-evidence and medical-revision gates, so it is
    written first: a trace describing reads whose result was refused would be a
    record of work that produced no governed output.
    """
    for contract_name, schema_name, data in (
        (RESULT_CONTRACT, RESULT_SCHEMA, result),
        (TRACE_CONTRACT, TRACE_SCHEMA, trace),
    ):
        data_file = _temp_json(data)
        try:
            _dao_write([
                "write-contract", case_id, contract_name,
                "--data-file", str(data_file), "--schema-name", schema_name,
                "--held-by", held_by, "--run-id", run_id, "--stage", STAGE,
            ])
        finally:
            data_file.unlink(missing_ok=True)


def require_open_attempt(case_id: str, run_id: str) -> None:
    """T13: the orchestrator owns run-state; this driver only checks it."""
    state = _dao_json(
        ["read-contract", case_id, "_run_state.json", "--run-id", run_id])
    if not any(
        item.get("stage_name") == STAGE and item.get("status") == "in_progress"
        for item in (state or {}).get("stages", [])
    ):
        raise RuntimeError(
            "BLOCKED: claim_analysis must be in_progress; the orchestrator owns "
            "attempt state")


def require_enabled(config: Mapping[str, Any]) -> None:
    if not routing.routing_enabled(config):
        raise RuntimeError(
            "BLOCKED: selective claim analysis is disabled "
            "(behavior_enabled=false). Activation requires a recorded "
            "`activation` block in the routing config.")


def _policy_page_text(
    case_id: str,
    pages_by_document: Mapping[str, Sequence[int]],
    *,
    run_id: str | None = None,
) -> dict:
    """Processed text for exactly the policy pages a clause check needs.

    Not the policy documents -- the PAGES. The index has already said which
    page each candidate clause sits on, so the read is narrowed with
    `--pages DOC=n,m` and never pulls a whole 약관 bundle. That matters for a
    reason beyond tidiness: a policy bundle is routinely the largest artifact
    in a case (on CASE_027 two of them were 81% of the stage's input while five
    pages were cited), and a whole-bundle read makes the trace unable to say
    how much of it the stage actually needed.

    `--run-id` is passed so the read is attributed to this run rather than
    landing in unattributed time, which is what makes "how much policy text did
    verification cost?" an answerable question instead of an estimate.

    An empty selection reads nothing at all: with no candidate clause there is
    no quote to verify, so there is no page worth fetching.
    """
    wanted = {document_id: list(pages)
              for document_id, pages in (pages_by_document or {}).items()
              if pages}
    if not wanted:
        return {}
    args = ["read-redacted-text-bundle", case_id]
    for document_id, pages in sorted(wanted.items()):
        args.append(f"--doc-id={document_id}")
        args.append(f"--pages={document_id}=" + ",".join(
            str(page) for page in sorted(pages)))
    if run_id:
        args.append(f"--run-id={run_id}")
    bundle = _dao_json(args, allow_missing=True)
    return _page_text_index(bundle or {"documents": []})


# ------------------------------------------- intake / administrative reads --
#
# **DEFERRED, and deliberately not implemented.** The three industrial-accident
# filing fields need an administrative source: whether a 산재 claim was filed,
# whether it was approved, and for which 상병. No stage in this repository
# produces one. There is no schema for such a declaration, no DAO subcommand
# that writes it, no `medical_document_kind` for an administrative form (and
# adding one would pollute Stage 2's classification contract), and no intake
# step that records it.
#
# An earlier revision read two filenames -- `_intake_declaration.json` and
# `_industrial_accident_filing.json` -- that nothing anywhere creates. Tests
# passed because their fixtures wrote the files themselves. That is a phantom
# contract: code shaped like an integration, with no producer at the other end,
# reporting a capability the pipeline does not have. Removed rather than left
# in place, because a reader cannot tell a phantom from a real seam.
#
# Until a real producer exists, the honest result is `unknown`:
#
# * filing status stays `unknown` for every case type, and
# * the three filing fields resolve `unavailable` with a reason naming the
#   missing input rather than a missing document.
#
# What must NOT happen in the meantime, and is the reason this is a comment
# rather than a heuristic: filing is never inferred from the accident. "Injured
# at work" makes the industrial case type applicable and says nothing whatever
# about whether a claim was filed. `not_filed` remains reachable only from a
# source that states it -- silence is `unknown`, permanently.
#
# Building it properly means one change carrying all of: a schema, DAO
# validation, a named writer, a real path from intake or a classified
# administrative document, exact-quote verification, a consumer, and an E2E
# test. See `open-decisions.md`.

FILING_ROUTE_DEFERRED_REASON = (
    "산재 접수 경로를 생성하는 단계가 이 파이프라인에 없습니다. 접수·승인·승인상병을 "
    "기록하는 단계가 없으므로 '없음'이 아니라 '확인 불가'로 둡니다 "
    "(보류 항목, open-decisions.md 참조)"
)

# The other half of the pair: the route never triggered at all. Named, not
# inline, so a test can ask WHICH reason this is without matching prose.
FILING_ROUTE_NOT_TRIGGERED_REASON = (
    "산재 접수 사실이나 해당 판정이 확인되지 않아 산재 경로가 활성화되지 않았습니다"
)


def filing_status_by_case_type() -> dict[str, str]:
    """Per-case-type filing status: empty while no producer exists.

    An empty mapping means every type reports `unknown`, which is what
    `assess_case_types` already does for a type it is given nothing about.
    Returning `not_filed` here would assert that no claim was filed -- a fact
    nobody recorded.
    """
    return {}


def make_quote_verifier(page_text: Mapping[tuple[str, int], str]):
    """Bind a clause quote to served processed text, or refuse it.

    A clause reference that cannot be located verbatim is dropped by the
    caller. This is the same rule the medical side uses, applied to the policy
    layer: the exact range is what makes a citation checkable, so a quote with
    no range has no standing.
    """

    def verify(document_id: str, page: int, quote: str) -> dict | None:
        text = page_text.get((document_id, page))
        if text is None or not quote:
            return None
        return build_reference(document_id, page, quote, text)

    return verify



# ------------------------------------------------------------------- run --

def pages_by_document(bundle: Mapping[str, Any]) -> dict[str, list[dict]]:
    return {
        document["document_id"]: list(document.get("pages") or [])
        for document in bundle.get("documents") or []
    }


def run(
    *,
    case_id: str,
    run_id: str,
    held_by: str,
    provider,
    medical_revision_context: Mapping[str, Any] | None = None,
) -> dict:
    """The whole stage: plan, read once per document, resolve, publish.

    Reads and writes go through the DAO. Run state is not touched -- T13 keeps
    update-run-state and finalize-stage with the orchestrator, so this refuses
    to start unless the orchestrator already opened the attempt.
    """
    config = routing.load_routing_config()
    require_enabled(config)
    require_open_attempt(case_id, run_id)

    # `document_manifest.json`, with no leading underscore. The underscore
    # prefix marks the DAO's own bookkeeping files (_run_state, _source_ledger,
    # _conflict_ledger); the manifest is a contract and does not carry it.
    # Spelled with one here until 2026-08-20, which made this driver fail at its
    # first DAO read on every case -- invisible while the lane was disabled,
    # because nothing ever reached this line.
    manifest = _dao_json(["read-contract", case_id, "document_manifest.json",
                          "--run-id", run_id])
    documents = classified_documents(manifest)
    page_text: dict[tuple[str, int], str] = {}
    loaded_pages: dict[str, list[dict]] = {}

    def load_document_pages(document_id: str) -> list[dict]:
        """Fetch one document only after the field scheduler selects it."""
        if document_id in loaded_pages:
            return loaded_pages[document_id]
        bundle = _dao_json([
            "read-redacted-text-bundle", case_id,
            f"--doc-id={document_id}", f"--run-id={run_id}",
        ]) or {"documents": []}
        pages = pages_by_document(bundle).get(document_id, [])
        loaded_pages[document_id] = pages
        page_text.update(_page_text_index(bundle))
        return pages

    extract = extraction.make_reader(provider, load_document_pages)
    observation_ids = _observation_id_sequence()
    # No `non_medical_reader`: nothing in this pipeline produces an
    # administrative filing source, so the route resolves `unavailable` with
    # the deferred reason rather than reading a file that does not exist.
    outcomes, cache = extract_all(
        config=config, documents=documents, extract=extract,
        page_text=page_text, observation_ids=observation_ids)

    # The shape here is the trace schema's, not a convenient one: `disposition`
    # has no `not_read` member (an unread document is `skipped`), `wave` and
    # `reason` and `field_ids` are required, and the kind field is
    # `medical_document_kind`. The previous shape (`document_kind`,
    # `provider_calls`, `disposition: not_read`) failed validation on all four
    # counts, so every real trace write would have been refused -- invisible in
    # tests, because they assembled the dict and asserted on it without ever
    # validating it against the schema it is written under.
    dispositions = []
    presence_only = selection.presence_only_kinds(config)
    fields_by_document = selection.document_field_map(
        selection.plan_wave(config, documents, "A")
        + selection.plan_wave(config, documents, "B"))
    for ref in documents:
        routed = sorted({field_id for field_id, _rank
                         in fields_by_document.get(ref.document_id, [])})
        if ref.kind in presence_only:
            disposition, wave = "presence_only", "not_read"
            reason = ("a cost document: its presence answers the checklist, "
                      "and it is never opened for content")
        elif ref.ambiguous:
            disposition, wave = "ambiguous", "not_read"
            reason = "Stage 2 could not resolve a form kind, so it is not routed"
        elif cache.was_read(ref.document_id):
            disposition = "read"
            wave = "A" if any(
                outcome.grade == "A" and outcome.field_id in set(routed)
                for outcome in outcomes) else "B"
            reason = (f"opened once for {len(routed)} routed field(s)"
                      if routed else "opened for a routed field")
        elif ref.kind is None:
            disposition, wave = "skipped", "not_read"
            reason = "no medical classification, so no route reaches it"
        else:
            disposition, wave = "skipped", "not_read"
            reason = ("every field routed here stopped at a higher-priority "
                      "source before reaching it" if routed else
                      "no field routes to this document kind")
        dispositions.append({
            "document_id": ref.document_id,
            "disposition": disposition,
            "wave": wave,
            "reason": reason,
            "field_ids": routed,
            **({"medical_document_kind": ref.kind} if ref.kind else {}),
        })

    # Policy linking reads the layer `policy_clause_processing` already built.
    # A missing index is not an error here -- it means the case has no
    # processed policy to link against, which the links record as not_found.
    index = _dao_json(["read-document-index", case_id, "--run-id", run_id],
                      allow_missing=True)
    def _interim(rows):
        """Facts as the case-type assessor reads them.

        Observations are carried, not just the status: liability now reads
        `liability_opinion_conclusion`'s VALUE (성립/불성립), so a status-only
        view would make every legal opinion invisible to the verdict.
        """
        return [
            {"field_id": outcome.field_id,
             "resolution_status": outcome.status,
             "selected_observation_ids": list(outcome.selected_ids),
             "observations": list(outcome.observations)}
            for outcome in rows
        ]

    # ------------------------------------------------------------ stage 3-a --
    # The type-conditional round runs AFTER a first assessment -- which types
    # are in play decides which sources are worth opening -- and BEFORE the
    # verdicts that get published, since a field it settles can change one.
    first_pass = case_types_mod.assess_case_types(
        _interim(outcomes), filing_status_by_type=filing_status_by_case_type())
    settled, type_conditional_calls, type_conditional_plan = extract_additional(
        config=config, documents=documents, assessments=first_pass,
        outcomes_by_field={o.field_id: o for o in outcomes}, extract=extract,
        page_text=page_text, observation_ids=observation_ids)
    # The dispositions above were computed BEFORE this round ran, so every
    # document stage 3-a opened is still labelled `skipped / not_read`. Restamp
    # them from the plan: the trace and the result must not disagree about
    # whether a document was read (CASE_713 had DOC_006/007/008 recorded as
    # unread while their quotes sat in `claim_facts`). `wave` gets the schema's
    # `type_conditional` member, which existed for this and had no writer.
    _restamp_type_conditional(dispositions, type_conditional_plan)
    if settled:
        replaced = {outcome.field_id: outcome for outcome in settled}
        outcomes = [replaced.pop(outcome.field_id, outcome)
                    for outcome in outcomes]
        # A field the common pass never planned -- every `liability_basis`
        # field is on no medical route -- joins the list rather than vanishing.
        outcomes.extend(replaced.values())
    # A field this round ASKED FOR but found nothing in was read, not skipped.
    # The common pass had already stamped it `source_document_missing` with
    # "the field is not routed to any document source", which is true of the
    # medical routes and false of what actually happened: stage 3-a opened the
    # 법률의견서 and 보험사 문서 that `additional_fields_by_case_type` names for
    # this case type. On CASE_704 `comparative_negligence_rate` therefore told
    # a 손해사정사 the field was unroutable, while DOC_008 carried a whole
    # 「피해자의 과실비율」 section that declined to set a rate -- which is a
    # records finding, not a routing defect. `not_mentioned` is the honest
    # code: sources WERE read and none stated the value.
    #
    # Gated on whether the round actually OPENED anything, not merely on which
    # fields were eligible. A case with no `legal_opinion` and no
    # `insurer_response` makes every liability field eligible and gives the
    # round nothing to read, so `type_conditional_calls` is 0 -- and the
    # earlier shape rewrote the reason anyway. CASE_712 (2026-08-21) then
    # printed 「사건유형별 추가 확인 출처를 열람했으나 이 항목을 기재한 내용이
    # 없습니다」 on six fields while the trace recorded
    # `type_conditional_documents_read: 0`. Prose asserting a read that never
    # happened is worse than the stale reason it replaced: it tells a
    # 손해사정사 the sources were checked and empty when no such source is in
    # the pack at all, which is the very distinction section 7 exists to draw.
    asked_for = (additional_mod.eligible_field_ids(
        config, first_pass, {o.field_id: o for o in outcomes})
        if type_conditional_calls else {})
    for outcome in outcomes:
        if (outcome.field_id in asked_for
                and outcome.status == "unavailable"
                and outcome.unavailable_reason == "source_document_missing"):
            outcome.unavailable_reason = "not_mentioned"
            outcome.reason = (
                "사건유형별 추가 확인 출처를 열람했으나 이 항목을 기재한 "
                "내용이 없습니다")

    interim = _interim(outcomes)
    # Decided once, here, and handed to BOTH consumers. Previously this map
    # was built with empty lists, so every requirement resting on a disputed
    # fact published `supported` and cited no candidate -- the disagreement
    # existed in `conflict_candidates` and was invisible where a reviewer
    # judging the clause would look for it.
    conflicts_by_field = assign_conflict_candidate_ids(outcomes)
    # Pages first, text second. The index names the candidate clauses and the
    # page each sits on, so only those pages are fetched -- never the whole
    # policy bundle, which was the previous behaviour and made the stage's real
    # policy-read cost unmeasurable.
    # The case TYPE justifies a search of its own, so it has to be known before
    # the links are built rather than only inside `build_result`. A liability
    # policy prints none of the medical coverage terms -- searching only on
    # facts found nothing in CASE_053 while the index held both clauses the
    # dispute turns on. `assess_case_types` is pure over the facts, so
    # computing it here and again in `build_result` yields the same verdicts.
    link_assessments = case_types_mod.assess_case_types(
        interim, filing_status_by_type=filing_status_by_case_type())
    policy_pages = policy_link_builder.candidate_pages(
        claim_facts=interim, manifest=manifest, index=index,
        config=config, case_type_assessment=link_assessments)
    policy_text = _policy_page_text(case_id, policy_pages, run_id=run_id)
    policy_links = policy_link_builder.build_policy_links(
        claim_facts=interim, manifest=manifest, index=index,
        verify_quote=make_quote_verifier(policy_text),
        conflict_candidate_ids_by_field=conflicts_by_field,
        config=config, case_type_assessment=link_assessments,
    )

    result = build_result(
        case_id=case_id, run_id=run_id, outcomes=outcomes, config=config,
        documents=documents, medical_revision_context=medical_revision_context,
        model_name=getattr(provider, "model_name", "unknown"),
        policy_links=policy_links,
        filing_status_by_type=filing_status_by_case_type(),
        conflict_candidate_ids_by_field=conflicts_by_field,
    )
    cited = policy_link_builder.referenced_documents(policy_links)
    if cited:
        # An artifact that cites the policy layer records which version of it.
        # The DAO recomputes this at write time and refuses a stale one.
        snapshot = _dao_json(
            ["policy-snapshot", case_id]
            + [f"--document-id={doc}" for doc in cited]
            + ["--run-id", run_id])
        if snapshot is not None:
            result["upstream_policy_snapshot"] = snapshot
    trace = build_trace(
        case_id=case_id, run_id=run_id, config=config, outcomes=outcomes,
        document_dispositions=dispositions,
        provider_calls=sum(cache.calls.values()),
        type_conditional_documents=type_conditional_calls,
        type_conditional_calls=type_conditional_calls,
    )
    # Recorded so the policy-verification read scope is a number in the trace
    # rather than something a reader has to infer from the code.
    trace["metrics"]["policy_pages_read"] = sum(
        len(pages) for pages in policy_pages.values())
    trace["metrics"]["policy_documents_touched"] = len(policy_pages)
    publish(case_id=case_id, run_id=run_id, held_by=held_by,
            result=result, trace=trace)
    return {
        "documents_considered": len(dispositions),
        "documents_read": sum(1 for row in dispositions
                              if row["disposition"] == "read"),
        "provider_calls": sum(cache.calls.values()),
        "conflict_candidates": len(result["conflict_candidates"]),
        "asserted_fields": sum(1 for row in result["claim_facts"]
                               if row["resolution_status"] == "asserted"),
        "policy_links": len(policy_links),
        "policy_links_matched": sum(
            1 for link in policy_links
            if link["clause_link_status"] == "matched"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_id")
    parser.add_argument("--held-by", required=True)
    parser.add_argument("--run-id", required=True)
    add_provider_args(parser)
    args = parser.parse_args(argv)
    trace_mod.configure_from_args(args)
    try:
        provider = build_provider(parse_provider_config(args))
        print(json.dumps(run(case_id=args.case_id, run_id=args.run_id,
                             held_by=args.held_by, provider=provider),
                         ensure_ascii=False, sort_keys=True))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
