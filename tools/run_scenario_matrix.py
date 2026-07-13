"""Scenario matrix: exercises P8's resolution decision on a real
disagreement, using fork_case.py so real OCR only runs ONCE, no matter how
many scenarios are compared.

Scope, deliberately narrow -- not literal all-combinations. This covers
exactly one decision point: once checkpoint 1 finds a real disagreement,
what happens under each of the three ways it can be resolved (reading_a /
reading_b / left unresolved). That is the one gate in the pipeline whose
OUTCOME depends on real, non-deterministic LLM output and is genuinely
expensive to re-derive per branch -- forking it is the actual efficiency
win fork_case.py exists for.

Every OTHER gate (D2 approve/reject, P6 resolved/false_positive, P4's
three-way schema-failure handling, the read-modify-write locking behavior)
is structural DAO logic that does not depend on real OCR output at all.
Those are already covered exhaustively and cheaply by tests/test_dao_*.py
(133 deterministic unit tests, zero real LLM calls, runs in under a
second) -- re-deriving that coverage here by forking real cases would just
be a slower, more expensive way to prove what those tests already prove.
If you need a NEW gate's combinations covered, extend the pytest suite,
not this script.

Also does not fabricate a disagreement if the real document doesn't have
one -- if checkpoint 1 finds full agreement, this reports that and exits;
manufacturing a fake disagreement to have something to branch on would
defeat the point of testing something real.

Usage:
    python tools/run_scenario_matrix.py CASE_ID DOC_ID <path to raw pdf> --held-by NAME
"""
import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from dao import case_dir, load_run_state
from fork_case import next_free_case_id, copy_outputs_and_rewrite_case_id, copy_data_tree, check_no_active_locks
from run_checkpoint1 import run_checkpoint1, resolve_from_raw_ocr

ROOT = Path(__file__).resolve().parent.parent

SCENARIOS = ["reading_a", "reading_b", "unresolved"]


def _fork_for_scenario(source_case_id: str, label: str) -> str:
    source_root = case_dir(source_case_id)
    check_no_active_locks(source_root)
    new_case_id = next_free_case_id()
    dest = case_dir(new_case_id)
    if any(dest.iterdir()):
        sys.exit(f"error: {dest} unexpectedly non-empty -- refusing to fork into it")
    warnings = copy_outputs_and_rewrite_case_id(source_root, new_case_id)
    if warnings:
        sys.exit(f"error: fork for scenario {label!r} produced schema warnings: {warnings}")
    copy_data_tree("processed", source_case_id, new_case_id)
    return new_case_id


def run_matrix(case_id: str, doc_id: str, pdf_path: str, held_by: str, run_id: str) -> dict:
    baseline = run_checkpoint1(case_id, doc_id, pdf_path, held_by, run_id)

    if baseline["status"] != "blocked_disagreement":
        return {"disagreement_found": False, "baseline": baseline,
                "note": "No real disagreement on this document -- nothing to branch on. "
                        "Not fabricating one; this only tests real outcomes."}

    ocr_data = json.loads(Path(baseline["raw_ocr_path"]).read_text(encoding="utf-8"))
    page = baseline["disagreed_pages"][0]
    if len(baseline["disagreed_pages"]) > 1:
        print(f"note: {len(baseline['disagreed_pages'])} pages disagreed -- "
              f"scenario matrix only branches on page {page}, the first one.", file=sys.stderr)

    results = {}
    for scenario in SCENARIOS:
        fork_id = _fork_for_scenario(case_id, scenario)
        if scenario == "unresolved":
            manifest = json.loads((case_dir(fork_id) / "document_manifest.json").read_text(encoding="utf-8"))
            state = load_run_state(fork_id)
            results[scenario] = {
                "fork_case_id": fork_id, "status": "left_unresolved",
                "document_manifest_ocr_status": manifest["documents"][0]["ocr_status"],
                "run_state_stages": [s["status"] for s in state["stages"]],
            }
            continue
        outcome = resolve_from_raw_ocr(
            fork_id, doc_id, ocr_data, page=page, chosen_reading=scenario,
            resolved_by=f"scenario-matrix:{scenario}",
            note=f"Automated scenario-matrix run -- forces {scenario} to observe the pipeline's behavior, "
                 f"not a real verified resolution. Do not treat this fork's output as a trustworthy case.",
            held_by=held_by, run_id=run_id,
        )
        outcome["fork_case_id"] = fork_id
        results[scenario] = outcome

    return {"disagreement_found": True, "baseline_case_id": case_id, "page": page, "scenarios": results}


def print_summary(matrix: dict) -> None:
    if not matrix["disagreement_found"]:
        print(matrix["note"])
        return
    print(f"\nPage {matrix['page']} disagreement -- {len(matrix['scenarios'])} scenario(s), "
          f"real OCR ran once (case {matrix['baseline_case_id']}):\n")
    print(f"{'scenario':<12} {'fork case_id':<14} {'status':<20} detail")
    print("-" * 70)
    for name, r in matrix["scenarios"].items():
        if r.get("status") == "left_unresolved":
            detail = f"manifest ocr_status={r['document_manifest_ocr_status']}, stages={r['run_state_stages']}"
        elif r.get("status") == "passed":
            detail = f"document_type={r.get('document_type')}, cross_validation_status={r.get('cross_validation_status')}"
        else:
            detail = json.dumps({k: v for k, v in r.items() if k != "fork_case_id"}, ensure_ascii=False)
        print(f"{name:<12} {r['fork_case_id']:<14} {r.get('status', ''):<20} {detail}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("case_id")
    ap.add_argument("doc_id")
    ap.add_argument("pdf_path")
    ap.add_argument("--held-by", required=True)
    ap.add_argument("--run-id", required=True)
    args = ap.parse_args()

    matrix = run_matrix(args.case_id, args.doc_id, args.pdf_path, args.held_by, args.run_id)
    print_summary(matrix)


if __name__ == "__main__":
    main()
