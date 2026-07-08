"""POC 케이스 폴더를 파이프라인 입력으로 복사하면서 정답지를 격리한다.

- 모델 입력용 문서 → data/raw/CASE_XXX/
- 정답지(최종 손해사정서·지급 결과) → data/ground_truth/CASE_XXX/
  (critic-evaluation 에이전트만 접근 가능)

POC/는 절대 수정하지 않는다 (복사만). 기본은 dry-run으로 분류 계획만
출력하며, 사람이 분류를 확인한 뒤 --yes로 실행한다 — 정답지 격리는
구조로 강제해야 하는 규칙이므로 자동 실행하지 않는다.

파일 하나에 여러 문서가 섞인 경우(예: 손해사정서 본문 + 증빙자료가 한
PDF) --split으로 페이지 범위를 나눠 각각 다른 계층으로 보낸다. 분할
명세에 포함된 파일은 파일명 패턴 분류를 거치지 않으며, 명세에 없는
페이지는 복사되지 않는다 (dry-run에 "제외"로 표시).

사용법:
    python tools/intake_case.py "POC/후유장해 케이스" CASE_003
    python tools/intake_case.py "POC/후유장해 케이스" CASE_003 --yes
    python tools/intake_case.py "POC/..." CASE_001 --ground-truth "*손해사정서*" "*지급*" --yes
    python tools/intake_case.py "POC/후유장해 케이스" CASE_003 \
        --files "배상-상완골*" \
        --split "배상-상완골 근위부 골절OP (김태윤) - 고객정보 삭제.pdf:1-13=ground_truth,14-110=raw"
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


def parse_split_spec(spec):
    """'파일명.pdf:1-13=ground_truth,14-110=raw' → (파일명, [(1, 13, dest), ...])

    페이지 번호는 1부터, 범위 양끝 포함. Windows 파일명에 ':'가 올 수
    없으므로 마지막 ':' 기준으로 나눈다.
    """
    if ":" not in spec:
        sys.exit(f"오류: --split 형식은 '파일명:시작-끝=계층,...' — {spec}")
    fname, ranges_part = spec.rsplit(":", 1)
    ranges = []
    for part in ranges_part.split(","):
        try:
            rng, dest = part.split("=")
            start, end = (int(x) for x in rng.split("-"))
        except ValueError:
            sys.exit(f"오류: --split 범위 형식은 '시작-끝=계층' — {part!r}")
        if dest not in ("raw", "ground_truth"):
            sys.exit(f"오류: --split 계층은 raw|ground_truth — {dest!r}")
        if not 1 <= start <= end:
            sys.exit(f"오류: --split 페이지 범위가 이상함 — {part!r}")
        ranges.append((start, end, dest))
    ranges.sort()
    for (_, e1, _), (s2, _, _) in zip(ranges, ranges[1:]):
        if s2 <= e1:
            sys.exit(f"오류: --split 범위가 겹침 — {fname}")
    return fname, ranges


def split_output_name(src, start, end):
    return f"{src.stem}__p{start:03d}-{end:03d}{src.suffix}"


def main():
    ap = argparse.ArgumentParser(description="POC 케이스 → 파이프라인 입력 복사 (정답지 격리)")
    ap.add_argument("case_dir", help="POC 케이스 폴더 경로")
    ap.add_argument("case_id", help="예: CASE_001")
    ap.add_argument("--ground-truth", nargs="+", default=DEFAULT_GT_PATTERNS,
                    metavar="PATTERN", help=f"정답지 파일명 패턴 (기본: {DEFAULT_GT_PATTERNS})")
    ap.add_argument("--files", nargs="+", metavar="PATTERN",
                    help="이 패턴에 맞는 파일만 intake (기본: 폴더 전체)")
    ap.add_argument("--split", nargs="+", default=[], metavar="SPEC",
                    help="페이지 범위 분할: '파일명:1-13=ground_truth,14-110=raw'")
    ap.add_argument("--yes", action="store_true", help="분류 계획대로 실제 복사 실행")
    args = ap.parse_args()

    case_dir = Path(args.case_dir)
    if not case_dir.is_dir():
        sys.exit(f"오류: 케이스 폴더 없음 — {case_dir}")
    if not args.case_id.startswith("CASE_"):
        sys.exit("오류: case_id는 CASE_ 접두사 필요 (예: CASE_001)")

    files = sorted(p for p in case_dir.rglob("*") if p.is_file())
    if args.files:
        files = [f for f in files
                 if any(fnmatch.fnmatch(f.name, pat) for pat in args.files)]
        if not files:
            sys.exit(f"오류: --files 패턴에 맞는 파일 없음 — {args.files}")

    # 분할 명세 검증 (페이지 수 확인까지 dry-run에서 미리 수행)
    splits = {}  # 파일명 → (Path, ranges, page_count)
    for spec in args.split:
        fname, ranges = parse_split_spec(spec)
        src = next((f for f in files if f.name == fname), None)
        if src is None:
            sys.exit(f"오류: --split 대상 파일이 intake 목록에 없음 — {fname}")
        try:
            import fitz  # pymupdf — 분할 사용 시에만 필요
        except ImportError:
            sys.exit("오류: --split에는 pymupdf 필요 — pip install pymupdf")
        page_count = fitz.open(src).page_count
        if ranges[-1][1] > page_count:
            sys.exit(f"오류: --split 범위가 총 {page_count}p를 벗어남 — {fname}")
        splits[fname] = (src, ranges, page_count)

    plan = classify([f for f in files if f.name not in splits], args.ground_truth)

    gt = [f for f, dest in plan if dest == "ground_truth"]
    raw = [f for f, dest in plan if dest == "raw"]

    print(f"케이스: {case_dir.name} → {args.case_id}")
    print(f"\n[모델 입력 → data/raw/{args.case_id}/]  통짜 {len(raw)}건")
    for f in raw:
        print(f"  - {f.name}")
    print(f"\n[정답지 격리 → data/ground_truth/{args.case_id}/]  통짜 {len(gt)}건")
    for f in gt:
        print(f"  - {f.name}")

    for fname, (src, ranges, page_count) in splits.items():
        print(f"\n[분할] {fname} (총 {page_count}p)")
        covered = set()
        for start, end, dest in ranges:
            covered.update(range(start, end + 1))
            print(f"  - p{start}-{end} → {dest}/{split_output_name(src, start, end)}")
        excluded = sorted(set(range(1, page_count + 1)) - covered)
        if excluded:
            print(f"  - 제외 (복사 안 함): {len(excluded)}p — {excluded}")

    has_gt = gt or any(d == "ground_truth" for _, rs, _ in splits.values() for *_, d in rs)
    if not has_gt:
        print("\n경고: 정답지로 분류된 파일이 0건입니다. 패턴을 확인하세요"
              " — 정답지가 raw로 새어 들어가면 평가가 오염됩니다.")

    if not args.yes:
        print("\n(dry-run) 분류가 맞으면 --yes를 붙여 다시 실행하세요.")
        return

    raw_dir = ROOT / "data" / "raw" / args.case_id
    gt_dir = ROOT / "data" / "ground_truth" / args.case_id
    dest_dirs = {"raw": raw_dir, "ground_truth": gt_dir}
    for d in (raw_dir, gt_dir):
        d.mkdir(parents=True, exist_ok=True)
    for f in raw:
        shutil.copy2(f, raw_dir / f.name)
    for f in gt:
        shutil.copy2(f, gt_dir / f.name)

    split_records = []
    for fname, (src, ranges, page_count) in splits.items():
        import fitz
        doc = fitz.open(src)
        for start, end, dest in ranges:
            out = fitz.open()
            out.insert_pdf(doc, from_page=start - 1, to_page=end - 1)
            out_name = split_output_name(src, start, end)
            out.save(dest_dirs[dest] / out_name)
            out.close()
            split_records.append({"source": fname, "pages": f"{start}-{end}",
                                  "dest": dest, "output": out_name})
        doc.close()

    # 감사 추적: 어떤 파일이 어디로 갔는지 기록
    record = {
        "case_id": args.case_id,
        "source": str(case_dir),
        "copied_at": datetime.now(KST).isoformat(),
        "ground_truth_patterns": args.ground_truth,
        "file_patterns": args.files,
        "raw": [f.name for f in raw],
        "ground_truth": [f.name for f in gt],
        "splits": split_records,
    }
    (raw_dir / "_intake_record.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n복사 완료. 기록: {raw_dir / '_intake_record.json'}")


if __name__ == "__main__":
    main()
