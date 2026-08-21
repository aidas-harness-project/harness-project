"""A cost document is blocked for amounts, not for the dates only it carries.

Measured on CASE_705 against CASE_907, same source material. The legacy lane
read the 진료비 세부산정내역 whole and extracted `surgery_or_procedure_date`
(2023-12-05), `imaging_date` (2023-12-04) and `treatment_period`'s end date
(2024-06-28). The selective lane blocked those documents entirely under
`presence_only` and lost all three.

They were not lost to a bad read. They were never read: searching the whole
case for "2024-06-28" returns exactly ONE hit and it is on a receipt, and the
clinical note says only `Plan> admission, 내일 Op.` -- "surgery tomorrow", no
date. A 진료비 세부산정내역 is `date + 수가코드 + 항목명 + 금액` per line,
which makes it the densest treatment-date ledger in a case and often the only
place a date exists at all.

So the block narrows from "never open" to "never open FOR AN AMOUNT". Amount
extraction stays out of PoC scope, and no amount field is on the allow-list.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import claim_analysis_selection as selection  # noqa: E402

CONFIG = json.loads(
    (ROOT / "config" / "claim_analysis"
     / "claim_analysis_routing_v0.1.json").read_text(encoding="utf-8"))
FIELDS = {row["field_id"]: row for row in CONFIG["fields"]}

CLINICAL = selection.DocumentRef("DOC_014", "progress_record")
COST_A = selection.DocumentRef("DOC_019", "medical_expense_itemization")
COST_B = selection.DocumentRef("DOC_024", "medical_expense_itemization")


def _plan(field_id, documents):
    return selection.plan_field(FIELDS[field_id], CONFIG, documents)


def test_a_date_field_can_reach_a_cost_document():
    plan = _plan("surgery_or_procedure_date", [CLINICAL, COST_A, COST_B])
    reached = {doc for step in plan.steps for doc in step.document_ids}
    assert {"DOC_019", "DOC_024"} <= reached, (
        "the only pages carrying the surgery date were unreachable")


def test_the_cost_document_is_the_last_rung():
    """A 수술기록 stating its own date must beat a billing line that has one."""
    plan = _plan("surgery_or_procedure_date", [CLINICAL, COST_A])
    assert plan.steps[0].document_ids == ("DOC_014",)
    assert plan.steps[-1].document_ids == ("DOC_019",)
    assert plan.steps[-1].priority_rank > plan.steps[0].priority_rank


def test_a_non_date_field_still_cannot_read_a_cost_document():
    plan = _plan("primary_diagnosis", [CLINICAL, COST_A, COST_B])
    reached = {doc for step in plan.steps for doc in step.document_ids}
    assert "DOC_019" not in reached and "DOC_024" not in reached, (
        "presence_only must still hold for everything but the date fields")


def test_no_amount_field_is_on_the_allow_list():
    """Amounts remain out of PoC scope; adding one is a scope decision."""
    for field_id in selection.COST_DOCUMENT_DATE_FIELDS:
        assert not any(token in field_id for token in
                       ("amount", "expense", "cost", "total", "rate_paid")), (
            f"{field_id} looks like an amount field")


def test_a_case_with_no_cost_document_is_unaffected():
    plan = _plan("surgery_or_procedure_date", [CLINICAL])
    assert [s.document_ids for s in plan.steps] == [("DOC_014",)]
