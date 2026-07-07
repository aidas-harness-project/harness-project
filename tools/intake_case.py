"""POC 케이스 폴더를 파이프라인 입력으로 복사하면서 정답지를 격리한다.

- 모델 입력용 문서 → data/raw/CASE_XXX/
- 정답지(최종 손해사정서·지급 결과) → data/ground_truth/CASE_XXX/
  (critic-evaluation 에이전트만 접근 가능)

POC/는 절대 수정하지 않는다 (복사만). 기본은 dry-run으로 분류 계획만
출력하며, 사람이 분류를 확인한 뒤 --yes로 실행한다 — 정답지 격리는
구조로 강제해야 하는 규칙이므로 자동 실행하지 않는다.

사용법:
    python tools/intake_case.py "POC/후유장해 케이스" CASE_003
    python tools/intake_case.py "POC/후유장해 케이스" CASE_003 --yes
    python tools/intake_case.py "POC/..." CASE_001 --ground-truth "*손해사정서*" "*지급*" --yes
"""
import argparse
import fnmatch
import json
import shutil
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
KST = timezone(timedelta(hours=9))

# 정답지로 간주하는 기본 파일명 패턴 (평가 전용 — 모델 입력 금지)
DEFAULT_GT_PATTERNS = ["*손해사정서*", "*지급 근거*", "*지급내역*"]
IGNORE = {".DS_Store", "Thumbs.db"}


def classify(files, gt_patterns):
    plan = []
    for f in files:
        if f.name in IGNORE:
            continue
        is_gt = any(fnmatch.fnmatch(f.name, pat) for pat in gt_patterns)
        plan.append((f, "ground_truth" if is_gt else "raw"))
    return plan


def main():
    ap = argparse.ArgumentParser(description="POC 케이스 → 파이프라인 입력 복사 (정답지 격리)")
    ap.add_argument("case_dir", help="POC 케이스 폴더 경로")
    ap.add_argument("case_id", help="예: CASE_001")
    ap.add_argument("--ground-truth", nargs="+", default=DEFAULT_GT_PATTERNS,
                    metavar="PATTERN", help=f"정답지 파일명 패턴 (기본: {DEFAULT_GT_PATTERNS})")
    ap.add_argument("--yes", action="store_true", help="분류 계획대로 실제 복사 실행")
    args = ap.parse_args()

    case_dir = Path(args.case_dir)
    if not case_dir.is_dir():
        sys.exit(f"오류: 케이스 폴더 없음 — {case_dir}")
    if not args.case_id.startswith("CASE_"):
        sys.exit("오류: case_id는 CASE_ 접두사 필요 (예: CASE_001)")

    files = sorted(p for p in case_dir.rglob("*") if p.is_file())
    plan = classify(files, args.ground_truth)

    gt = [f for f, dest in plan if dest == "ground_truth"]
    raw = [f for f, dest in plan if dest == "raw"]

    print(f"케이스: {case_dir.name} → {args.case_id}")
    print(f"\n[모델 입력 → data/raw/{args.case_id}/]  {len(raw)}건")
    for f in raw:
        print(f"  - {f.name}")
    print(f"\n[정답지 격리 → data/ground_truth/{args.case_id}/]  {len(gt)}건")
    for f in gt:
        print(f"  - {f.name}")

    if not gt:
        print("\n경고: 정답지로 분류된 파일이 0건입니다. 패턴을 확인하세요"
              " — 정답지가 raw로 새어 들어가면 평가가 오염됩니다.")

    if not args.yes:
        print("\n(dry-run) 분류가 맞으면 --yes를 붙여 다시 실행하세요.")
        return

    raw_dir = ROOT / "data" / "raw" / args.case_id
    gt_dir = ROOT / "data" / "ground_truth" / args.case_id
    for d in (raw_dir, gt_dir):
        d.mkdir(parents=True, exist_ok=True)
    for f in raw:
        shutil.copy2(f, raw_dir / f.name)
    for f in gt:
        shutil.copy2(f, gt_dir / f.name)

    # 감사 추적: 어떤 파일이 어디로 갔는지 기록
    record = {
        "case_id": args.case_id,
        "source": str(case_dir),
        "copied_at": datetime.now(KST).isoformat(),
        "ground_truth_patterns": args.ground_truth,
        "raw": [f.name for f in raw],
        "ground_truth": [f.name for f in gt],
    }
    (raw_dir / "_intake_record.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n복사 완료. 기록: {raw_dir / '_intake_record.json'}")


if __name__ == "__main__":
    main()
