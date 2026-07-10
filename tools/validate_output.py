"""Schema validation gate for component output JSON, against schemas/.

Run this before handing a stage's output to the next stage. Schema is
resolved automatically from the filename: {name}.json -> schemas/{name}.schema.json
(trailing suffixes like _v2, _CASE_XXX are ignored).

For programmatic use (e.g. from dao.py's write-contract), import
validate_instance()/schema_name_for() from tools/_validation.py directly
rather than shelling out to this script.

Usage:
    python tools/validate_output.py outputs/CASE_001/screening_report.json
    python tools/validate_output.py outputs/CASE_001/*.json
    python tools/validate_output.py --all outputs/CASE_001

Exit codes: 0 = all passed, 1 = at least one failure, 2 = no schema found for any target (nothing validated).
Files with no schema yet are reported as SKIP.
"""
import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from _validation import load_registry, schema_name_for, validate_instance


def validate_file(json_path: Path, schemas, registry) -> str:
    """Return 'pass' | 'fail' | 'skip'."""
    name = schema_name_for(json_path)
    if name is None:
        print(f"SKIP {json_path} -- no matching schema (not yet defined)")
        return "skip"
    try:
        instance = json.loads(json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"FAIL {json_path} -- invalid JSON: {e}")
        return "fail"
    errors = validate_instance(instance, name, schemas, registry)
    if errors:
        print(f"FAIL {json_path} (schema: {name})")
        for e in errors:
            print(f"     - {e}")
        return "fail"
    print(f"PASS {json_path} (schema: {name})")
    return "pass"


def main():
    ap = argparse.ArgumentParser(description="Schema validation gate for component output JSON")
    ap.add_argument("paths", nargs="*", help="JSON file(s) to validate")
    ap.add_argument("--all", metavar="DIR", help="Validate every *.json under DIR")
    args = ap.parse_args()

    targets: list[Path] = [Path(p) for p in args.paths]
    if args.all:
        targets += sorted(Path(args.all).rglob("*.json"))
    if not targets:
        ap.error("specify at least one file to validate")

    schemas, registry = load_registry()
    results = {"pass": 0, "fail": 0, "skip": 0}
    for t in targets:
        if not t.exists():
            print(f"FAIL {t} -- file not found")
            results["fail"] += 1
            continue
        results[validate_file(t, schemas, registry)] += 1

    print(f"\nResult: PASS {results['pass']} / FAIL {results['fail']} / SKIP {results['skip']}")
    if results["fail"]:
        sys.exit(1)
    if results["pass"] == 0 and results["skip"]:
        sys.exit(2)


if __name__ == "__main__":
    main()
