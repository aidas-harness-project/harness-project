"""Turn a scorer's comparison rows into a schema-valid fidelity score.

The judgement stays the scorer's: whether a value matches, which of the answer
key's issues the report predicted, which documents it relied on. Everything
around that judgement stops being the scorer's -- the field universe, each
field's grade, whether it is decision-bearing, whether the pipeline is even
asked to extract it, the upstream account of an absence, the weights, the
arithmetic, and the verdict thresholds all come from the routing config, the
case's own contracts, and the rubric.

Why it exists: the first two scores were produced by hand-written scratch
scripts that lived outside the repository, over 15 rows each chosen after
reading the answer key, out of the 56 fields claim_analysis had emitted. Two
numbers taken that way are not taken with the same instrument. This tool makes
the instrument the same one every time, and fails loudly when a field the
pipeline resolved has no row -- deciding the key says nothing about a field is
still a judgement, but now it is one that has to be written down.

Input (--rows): JSON with
    {
      "case_id": "CASE_705",
      "case_types": ["liability", "personal_insurance"],
      "target_report_sha256": "…",
      "ground_truth_files": ["GT_001.pdf"],
      "fields": [ {field_id, screening_value, ground_truth_value, match_kind,
                   screening_absence_kind, screening_quote, gt_locator} … ],
      "issues": [ {gt_issue_id, gt_issue_label, predicted, screening_quote,
                   screening_section, gt_locator} … ],
      "documents": [ {document_kind, relied_on_by_ground_truth,
                      screening_classification, verdict, screening_quote,
                      gt_locator} … ],
      "conclusion_agreement": "screening_declined",
      "conclusion_note": "…",
      "out_of_universe_items": [ … ],
      "findings": [ … ]
    }

Usage:
    python tools/score_screening_fidelity.py --rows rows.json --run-id RUN_YYYYMMDD_N
        [--out result.json] [--quiet]
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "claim_analysis" / "claim_analysis_routing_v0.1.json"
OUTPUTS = ROOT / "outputs"

WEIGHTS = {"F1": 50, "F2": 30, "F3": 20, "F4": 0}
AGREED = {"exact", "normalized"}
COUNTED = AGREED | {"mismatch", "missing_in_screening"}
EXCLUDED = {"missing_in_ground_truth", "not_applicable", "preserved_without_adoption"}
DOC_COUNTED = {"correct", "under", "miss"}

SCHEMA_VERSION = "screening_fidelity_result.v0.1"
RUBRIC_VERSION = "screening_fidelity.v0.2"
IN_SCOPE = "in_scope"


def load_config():
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    return cfg, {f["field_id"]: f for f in cfg["fields"]}


def field_grade(row):
    """The field's grade from whichever axis it declares.

    Mirrors tools/claim_analysis_selection.field_grade: the config requires
    exactly one of the two, so there is never a default to invent. Merging the
    axes is what once ranked 과실비율 on what a physician needs.
    """
    grade = row.get("medical_advisory_grade")
    return grade if grade is not None else row["priority_grade"]


def is_deferred(row):
    return (row.get("extraction_wave") == "deferred"
            or row.get("activation_basis") == "deferred")


def upstream_status(case_id):
    """resolution_status / unavailable_reason per field, copied not inferred."""
    path = OUTPUTS / case_id / "claim_analysis_result.json"
    if not path.exists():
        return {}
    return {
        f["field_id"]: (f.get("resolution_status"), f.get("unavailable_reason"))
        for f in json.loads(path.read_text(encoding="utf-8")).get("claim_facts", [])
    }


def rate(items, hit):
    counted = [i for i in items if hit(i) is not None]
    if not counted:
        return None, 0, 0
    hits = sum(1 for i in counted if hit(i))
    return round(100 * hits / len(counted), 1), hits, len(counted)


def build(rows, run_id):
    cfg, by_id = load_config()
    case_id = rows["case_id"]
    upstream = upstream_status(case_id)

    problems = []
    comparisons = []
    for row in rows["fields"]:
        fid = row["field_id"]
        cfg_row = by_id.get(fid)
        if cfg_row is None:
            problems.append(f"{fid}: not a field_id in {cfg['config_version']}")
            continue
        match_kind = row["match_kind"]
        if match_kind == "preserved_without_adoption":
            if cfg_row.get("legal_authority") != "legal_opinion":
                problems.append(
                    f"{fid}: preserved_without_adoption is for a legal_opinion field "
                    f"(this one is {cfg_row.get('legal_authority')!r}) -- a report that "
                    "carried no value for an ordinary field did not preserve anything")
            elif not row.get("screening_quote"):
                problems.append(
                    f"{fid}: preserved_without_adoption needs the line that shows the "
                    "opinions were carried; without it the exemption is a way to spend "
                    "silence")
        if is_deferred(cfg_row) and match_kind not in EXCLUDED:
            problems.append(
                f"{fid}: the pipeline is not asked to extract it "
                f"(wave={cfg_row.get('extraction_wave')}), so it cannot be scored "
                f"{match_kind!r} -- use not_applicable")
        status, reason = upstream.get(fid, (None, None))
        comparisons.append({
            "field_id": fid,
            "field_name": cfg_row.get("label_ko"),
            "field_grade": field_grade(cfg_row),
            "core_field": bool(cfg_row.get("critical_conflict_field")),
            "legal_authority": cfg_row.get("legal_authority"),
            "screening_value": row.get("screening_value"),
            "ground_truth_value": row.get("ground_truth_value"),
            "match_kind": match_kind,
            "screening_absence_kind": row.get("screening_absence_kind", "not_applicable"),
            "pipeline_resolution_status": status,
            "pipeline_unavailable_reason": reason,
            "screening_quote": row.get("screening_quote"),
            "ground_truth_ref": {
                "file": row.get("gt_file", rows["ground_truth_files"][0]),
                "locator": row["gt_locator"],
            },
        })

    emitted = set(upstream)
    missing = sorted(emitted - {c["field_id"] for c in comparisons})
    if missing:
        problems.append(
            f"{len(missing)} field(s) claim_analysis resolved have no row: "
            + ", ".join(missing[:8]) + (" …" if len(missing) > 8 else ""))
    if problems:
        return None, problems

    f1, f1_hits, f1_n = rate(
        comparisons,
        lambda c: (c["match_kind"] in AGREED) if c["match_kind"] in COUNTED else None)
    # v0.2: the F2 denominator is in-scope issues only, on the same principle
    # that excludes a deferred field from F1 -- charging the report for work the
    # PoC never undertook measures the scope, not the report. Overall recall is
    # still reported beside it so the exclusion cannot quietly inflate the number.
    in_scope = [i for i in rows["issues"]
                if i.get("scope", IN_SCOPE) == IN_SCOPE]
    f2, f2_hits, f2_n = rate(in_scope, lambda i: bool(i["predicted"]))
    f2_all, f2_all_hits, f2_all_n = rate(rows["issues"], lambda i: bool(i["predicted"]))
    for issue in rows["issues"]:
        if issue.get("scope", IN_SCOPE) != IN_SCOPE and not issue.get("scope_reason"):
            problems.append(
                f"{issue['gt_issue_id']}: excluded from the F2 denominator with no "
                "scope_reason -- exclusion raises the score, so the ground for it "
                "has to be on the record")
    if problems:
        return None, problems
    f3, f3_hits, f3_n = rate(
        rows["documents"],
        lambda d: (d["verdict"] == "correct") if d["verdict"] in DOC_COUNTED else None)

    by_grade = {}
    for c in comparisons:
        if c["match_kind"] in COUNTED:
            g = by_grade.setdefault(c["field_grade"], [0, 0])
            g[1] += 1
            g[0] += 1 if c["match_kind"] in AGREED else 0
    grade_note = "; ".join(f"{g}: {h}/{n}" for g, (h, n) in sorted(by_grade.items()))

    core_mismatches = [c["field_id"] for c in comparisons
                       if c["core_field"] and c["match_kind"] == "mismatch"]

    dims = [
        {"dimension_id": "F1", "weight": WEIGHTS["F1"], "applicable": True,
         "na_reason": None, "score": f1,
         "rationale": (f"{cfg['config_version']}의 field_id로 대조. 분모 {f1_n}건 "
                       f"(등급별 {grade_note}). 정답지가 말하지 않는 필드와 deferred 필드는 "
                       f"행으로 남기되 분모에서 제외. 부재 사유는 claim_analysis_result에서 복사."),
         "screening_quotes": [c["screening_quote"] for c in comparisons
                              if c["screening_quote"]][:3] or ["(no quoted row)"],
         "fact_match_rate": round(f1_hits / f1_n, 3) if f1_n else None,
         "discretionary_agreement_rate": None,
         "field_comparisons": comparisons},
        {"dimension_id": "F2", "weight": WEIGHTS["F2"], "applicable": True,
         "na_reason": None, "score": f2,
         "rationale": (f"PoC 범위 내 쟁점 {f2_n}건 중 {f2_hits}건 예측 (v0.2). "
                       f"총괄 리콜은 {f2_all_hits}/{f2_all_n}. 범위 밖으로 뺀 쟁점은 "
                       f"{f2_all_n - f2_n}건이며 각각 scope_reason을 갖는다. "
                       "이 분모는 설정이 정해 주지 않는다 -- 채점자 열거로 남는 유일한 차원."),
         "screening_quotes": [i.get("screening_quote") for i in rows["issues"]
                              if i.get("screening_quote")][:2] or ["(no predicted issue)"],
         "issue_matches": [
             {"gt_issue_id": i["gt_issue_id"], "gt_issue_label": i["gt_issue_label"],
              "predicted": bool(i["predicted"]),
              **({"scope_reason": i["scope_reason"]} if i.get("scope_reason") else {}),
              "scope": i.get("scope", IN_SCOPE),
              "screening_quote": i.get("screening_quote"),
              "screening_section": i.get("screening_section"),
              "ground_truth_ref": {"file": rows["ground_truth_files"][0],
                                   "locator": i["gt_locator"]}}
             for i in rows["issues"]],
         "screening_only_issues": rows.get("screening_only_issues", [])},
        {"dimension_id": "F3", "weight": WEIGHTS["F3"], "applicable": True,
         "na_reason": None, "score": f3,
         "rationale": ("required_documents_by_case_type("
                       + ", ".join(rows["case_types"]) + ")의 document_kinds로 대조. "
                       f"분모 {f3_n}건."),
         "screening_quotes": [d.get("screening_quote") for d in rows["documents"]
                              if d.get("screening_quote")][:2] or ["(no quoted document)"],
         "document_comparisons": [
             {"document_kind": d["document_kind"],
              "relied_on_by_ground_truth": bool(d["relied_on_by_ground_truth"]),
              "screening_classification": d["screening_classification"],
              "verdict": d["verdict"], "screening_quote": d.get("screening_quote"),
              "ground_truth_ref": {"file": rows["ground_truth_files"][0],
                                   "locator": d["gt_locator"]}}
             for d in rows["documents"]]},
        {"dimension_id": "F4", "weight": 0, "applicable": True, "na_reason": None,
         "score": None, "rationale": rows["conclusion_note"],
         "screening_quotes": [rows.get("conclusion_quote", "(see F4 rationale)")],
         "conclusion_agreement": rows["conclusion_agreement"]},
    ]

    scored = [d for d in dims if d["score"] is not None]
    total = round(sum(d["score"] * d["weight"] for d in scored)
                  / sum(d["weight"] for d in scored), 1)
    verdict = ("divergent" if core_mismatches else
               "aligned" if total >= 80 else
               "partial" if total >= 60 else "divergent")

    result = {
        "case_id": case_id, "run_id": run_id, "component": "screening-fidelity",
        "status": "success", "created_at": rows["created_at"],
        "schema_version": SCHEMA_VERSION, "rubric_version": RUBRIC_VERSION,
        "routing_config_version": cfg["config_version"],
        "target_report_path": f"outputs/{case_id}/screening_report.md",
        "target_report_sha256": rows["target_report_sha256"],
        "ground_truth_access": {
            "version": "screening", "caller_stage": "screening_fidelity",
            "screening_stage_passed": True,
            "ground_truth_files": rows["ground_truth_files"]},
        "dimensions": dims, "fidelity_score": total,
        "core_field_mismatch": bool(core_mismatches), "verdict": verdict,
        "out_of_universe_items": rows.get("out_of_universe_items", []),
        "findings": rows.get("findings", []),
    }
    readout = {
        "F1": (f1, f1_hits, f1_n), "F2": (f2, f2_hits, f2_n), "F3": (f3, f3_hits, f3_n),
        "F2_overall": (f2_all, f2_all_hits, f2_all_n),
        "F4": rows["conclusion_agreement"], "total": total, "verdict": verdict,
        "core_mismatches": core_mismatches, "grades": grade_note,
        "rows": len(comparisons), "excluded": len(comparisons) - f1_n,
    }
    return (result, readout), []


def render(case_id, r):
    w = 62
    print("=" * w)
    print(f" {case_id}   fidelity {r['total']}   {r['verdict'].upper()}")
    print("=" * w)
    for dim in ("F1", "F2", "F3"):
        score, hits, n = r[dim]
        bar = "#" * int(round((score or 0) / 5)) if score is not None else ""
        print(f" {dim} {str(score):>5}  {hits:>3}/{n:<3} {bar}")
    o, oh, on = r["F2_overall"]
    print(f"    overall recall {o} ({oh}/{on}) -- {on - r['F2'][2]} issue(s) out of PoC scope")
    print(f" F4 {'':>5}  {r['F4']}  (excluded from the headline)")
    print("-" * w)
    print(f" rows {r['rows']} scored, {r['excluded']} excluded from the denominator")
    print(f" by grade: {r['grades']}")
    if r["core_mismatches"]:
        print(f" core-field mismatch pins the verdict: {', '.join(r['core_mismatches'])}")
    else:
        print(" no core-field mismatch")
    print("=" * w)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--rows", required=True, help="the scorer's comparison rows")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--out", help="where to write the result (default: beside --rows)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    rows = json.loads(Path(args.rows).read_text(encoding="utf-8"))
    built, problems = build(rows, args.run_id)
    if problems:
        print("BLOCKED: the rows do not describe a scorable comparison:")
        for p in problems:
            print(f"  - {p}")
        return 1
    result, readout = built

    # The case id belongs in the default name. Without it, scoring two cases in
    # one run silently overwrote the first result -- the same failure the DAO
    # write path was just taught to refuse, reintroduced one level up.
    out = Path(args.out) if args.out else Path(args.rows).with_name(
        f"{rows['case_id']}_screening_fidelity_result_{args.run_id}.json")
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    if not args.quiet:
        render(rows["case_id"], readout)
        print(f"\nwrote {out}")
        print("store it with:")
        print(f"  python tools/dao.py write-verification-result {rows['case_id']} "
              f"screening_fidelity_result_{args.run_id}.json \\\n"
              f"    --caller-stage screening_fidelity --version screening \\\n"
              f"    --data-file {out} --held-by screening-fidelity --run-id {args.run_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
