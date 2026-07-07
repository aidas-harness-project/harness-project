"""컴포넌트 출력 JSON을 schemas/의 JSON Schema로 검증하는 게이트.

에이전트 경계에서 산출물을 다음 단계로 넘기기 전에 반드시 실행한다.
스키마는 파일명으로 자동 매핑한다: {이름}.json → schemas/{이름}.schema.json
(뒤에 붙는 _v2, _CASE_XXX 등 접미사는 무시).

사용법:
    python tools/validate_output.py outputs/CASE_001/screening_report.json
    python tools/validate_output.py outputs/CASE_001/*.json
    python tools/validate_output.py --all outputs/CASE_001

종료 코드: 0 = 전부 통과, 1 = 실패 있음, 2 = 스키마 미존재(검증 불가).
스키마가 아직 없는 파일은 SKIP으로 보고한다 (Week 1→2→3 순 확정 중).
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = ROOT / "schemas"


def load_registry():
    schemas = {}
    for p in sorted(SCHEMA_DIR.glob("*.schema.json")):
        schemas[p.name] = json.loads(p.read_text(encoding="utf-8"))
    registry = Registry().with_resources(
        (name, Resource.from_contents(s)) for name, s in schemas.items()
    )
    return schemas, registry


def schema_name_for(json_path: Path) -> str | None:
    """파일명에서 스키마 이름 유도. 예: critic_result_v2.json → critic_result."""
    stem = json_path.stem
    stem = re.sub(r"_v\d+$", "", stem)          # critic_result_v2 → critic_result
    stem = re.sub(r"_CASE_\d+$", "", stem)      # 접미사형 케이스 ID 제거
    candidate = f"{stem}.schema.json"
    return candidate if (SCHEMA_DIR / candidate).exists() else None


def validate_file(json_path: Path, schemas, registry) -> str:
    """반환: 'pass' | 'fail' | 'skip'."""
    name = schema_name_for(json_path)
    if name is None:
        print(f"SKIP {json_path} — 대응 스키마 없음 (아직 미확정)")
        return "skip"
    try:
        instance = json.loads(json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"FAIL {json_path} — JSON 파싱 불가: {e}")
        return "fail"
    validator = Draft202012Validator(schemas[name], registry=registry)
    errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path))
    if errors:
        print(f"FAIL {json_path} (schema: {name})")
        for e in errors:
            loc = "/".join(map(str, e.absolute_path)) or "(root)"
            print(f"     - {loc}: {e.message}")
        return "fail"
    print(f"PASS {json_path} (schema: {name})")
    return "pass"


def main():
    ap = argparse.ArgumentParser(description="컴포넌트 출력 JSON 스키마 검증 게이트")
    ap.add_argument("paths", nargs="*", help="검증할 JSON 파일(들)")
    ap.add_argument("--all", metavar="DIR", help="디렉터리 내 모든 *.json 검증")
    args = ap.parse_args()

    targets: list[Path] = [Path(p) for p in args.paths]
    if args.all:
        targets += sorted(Path(args.all).rglob("*.json"))
    if not targets:
        ap.error("검증할 파일을 지정하세요")

    schemas, registry = load_registry()
    results = {"pass": 0, "fail": 0, "skip": 0}
    for t in targets:
        if not t.exists():
            print(f"FAIL {t} — 파일 없음")
            results["fail"] += 1
            continue
        results[validate_file(t, schemas, registry)] += 1

    print(f"\n결과: PASS {results['pass']} / FAIL {results['fail']} / SKIP {results['skip']}")
    if results["fail"]:
        sys.exit(1)
    if results["pass"] == 0 and results["skip"]:
        sys.exit(2)


if __name__ == "__main__":
    main()
