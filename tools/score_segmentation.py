"""Score a segmentation proposal against a hand-recorded ground-truth baseline.

Stage 1's build step 7 needs a number, not an eyeball: without one there is no
way to tell whether a crop-ratio or grid change helped or hurt. This reads a
proposal's boundary set (the page where each segment starts) and compares it to
the ground-truth boundary set parsed from a baseline markdown file, reporting
precision / recall / F1 plus the exact pages each got wrong.

A boundary is a page that STARTS a new document. The comparison is on that set
alone -- the same load-bearing idea merge_sheet_proposals uses: segment ranges
are fully determined by their start pages, so scoring the starts scores the
segmentation. Two proposals agree iff they start documents on the same pages.

The baseline file carries its boundaries in a fenced code block right after a
line containing "경계 페이지 목록" (or pass --baseline-boundaries explicitly).
The block is a comma/whitespace-separated list of page numbers. This keeps the
human-facing baseline table and the machine-read boundary set in one file, so
they cannot drift.

Usage:
    python tools/score_segmentation.py --proposal PATH --baseline PATH
    python tools/score_segmentation.py --proposal PATH --baseline-boundaries "1,14,15,..."
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")


def parse_baseline_boundaries(text: str) -> list[int]:
    """Extracts the ground-truth boundary page numbers from a baseline markdown.

    Looks for the first fenced code block after a heading/line mentioning
    '경계 페이지' and reads every integer out of it. Falls back to the first
    fenced block that is purely a number list if that marker is absent, so a
    baseline that just drops the numbers in a code block still works.
    """
    lines = text.splitlines()
    marker_idx = next(
        (i for i, line in enumerate(lines) if "경계 페이지" in line),
        None,
    )
    blocks = list(re.finditer(r"```[^\n]*\n(.*?)```", text, re.DOTALL))
    chosen = None
    if marker_idx is not None:
        marker_offset = sum(len(l) + 1 for l in lines[:marker_idx])
        chosen = next((b for b in blocks if b.start() >= marker_offset), None)
    if chosen is None:
        # Fall back to the first block that is only numbers/commas/whitespace.
        for b in blocks:
            body = b.group(1)
            if body.strip() and re.fullmatch(r"[\d,\s]+", body):
                chosen = b
                break
    if chosen is None:
        raise ValueError(
            "no boundary code block found -- add a fenced block after a "
            "'경계 페이지 목록' line, or pass --baseline-boundaries"
        )
    numbers = [int(n) for n in re.findall(r"\d+", chosen.group(1))]
    if not numbers:
        raise ValueError("the boundary code block contained no page numbers")
    return sorted(set(numbers))


def proposal_boundaries(proposal: dict) -> list[int]:
    """The set of pages the proposal starts a document on."""
    return sorted({seg["page_start"] for seg in proposal.get("segments", [])})


def score(predicted: list[int], truth: list[int]) -> dict:
    """Set precision / recall / F1 on boundary pages, with the mismatch lists.

    false_positives: pages the proposal called a boundary that the baseline did
    not (over-split). false_negatives: real boundaries the proposal missed
    (over-merge -- the costlier error, since a merge only surfaces after OCR).
    """
    pred, true = set(predicted), set(truth)
    tp = pred & true
    fp = sorted(pred - true)
    fn = sorted(true - pred)
    precision = len(tp) / len(pred) if pred else 0.0
    recall = len(tp) / len(true) if true else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "true_positives": len(tp),
        "predicted_count": len(pred),
        "truth_count": len(true),
        "false_positives_over_split": fp,
        "false_negatives_over_merge": fn,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--proposal", required=True, help="segmentation_proposal_*.json")
    parser.add_argument("--baseline", help="baseline markdown with a 경계 페이지 code block")
    parser.add_argument("--baseline-boundaries",
                        help="comma/space-separated ground-truth boundary pages (overrides --baseline)")
    args = parser.parse_args(argv)

    proposal = json.loads(Path(args.proposal).read_text(encoding="utf-8"))
    predicted = proposal_boundaries(proposal)

    if args.baseline_boundaries:
        truth = sorted({int(n) for n in re.findall(r"\d+", args.baseline_boundaries)})
    elif args.baseline:
        truth = parse_baseline_boundaries(Path(args.baseline).read_text(encoding="utf-8"))
    else:
        parser.error("pass --baseline or --baseline-boundaries")

    result = score(predicted, truth)
    result["predicted_boundaries"] = predicted
    result["truth_boundaries"] = truth
    result["unassigned_pages"] = proposal.get("unassigned_pages", [])
    print(json.dumps(result, ensure_ascii=False, indent=2))
    # Non-zero exit if anything is off, so a CI/scripted run can gate on it.
    return 0 if not result["false_positives_over_split"] and not result["false_negatives_over_merge"] else 1


if __name__ == "__main__":
    sys.exit(main())
