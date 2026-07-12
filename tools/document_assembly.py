"""Renders narrative documents (screening_report.md, draft_report_v*.md,
rebuttal_points.md) from agent-provided section content, and auto-generates
the [E#] citation tags plus the matching .evidence.json sidecar in one pass.

See harness-guardrails P1. No agent hand-writes a tag number or edits a
sidecar file directly -- an agent writes `{{E}}` as an inline placeholder
wherever a citation belongs, in the same order as its evidence_references
list for that section; this tool replaces each placeholder with a
sequentially-numbered [E#] tag and writes the sidecar from the same data,
so a tag and its citation can never drift out of sync.

Section-order/required-fields template rules are pending (see pipeline.md's
note) -- this tool renders whatever sections it's given, in the order
given. Template enforcement (which sections a given template_id requires)
gets layered on once that material arrives.

This writes into outputs/ like any other DAO write path -- locked
(held-by/run-id, same convention as dao.py write-contract, so dao.py
check-lock correctly sees a render in progress) and atomic. The generated
sidecar is schema-validated against evidence_sidecar.schema.json before
either file touches disk; a failure there is this tool's own bug (the
agent's evidence_references were already well-formed going in), not a data
problem to route around.

Input (--sections-file), one JSON object:
{
  "output_path": "outputs/CASE_003/draft_report_v1.md",
  "sections": [
    {"heading": "1. Case overview", "content": "...text with {{E}} placeholders...",
     "evidence_references": [{"document_id": "DOC_001", "page": 1, "quote": "..."}]}
  ]
}

Usage:
    python tools/document_assembly.py --sections-file /tmp/sections.json \\
        --held-by draft-report --run-id RUN_20260710_001
"""
import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from dao import acquire_lock_blocking, release_lock, atomic_write_text, atomic_write_json, now_iso
from _validation import load_registry, validate_instance

ROOT = Path(__file__).resolve().parent.parent


def render(spec: dict) -> tuple[str, dict]:
    output_path = spec["output_path"]
    lines = []
    citations = []
    tag_n = 0

    for section in spec["sections"]:
        lines.append(f"## {section['heading']}")
        lines.append("")
        content = section["content"]
        refs = section.get("evidence_references", [])
        placeholder_count = content.count("{{E}}")
        if placeholder_count != len(refs):
            raise ValueError(
                f"Section {section['heading']!r}: {placeholder_count} {{{{E}}}} placeholders "
                f"but {len(refs)} evidence_references -- these must match 1:1."
            )
        for ref in refs:
            tag_n += 1
            tag = f"E{tag_n}"
            content = content.replace("{{E}}", f"[{tag}]", 1)
            citation = {
                "tag": tag,
                "document_id": ref["document_id"],
                "quote": ref["quote"],
            }
            # evidence_sidecar.schema.json's page is integer-typed with no
            # null option -- omit the key entirely rather than writing
            # page: null when a reference doesn't have one.
            if ref.get("page") is not None:
                citation["page"] = ref["page"]
            citations.append(citation)
        lines.append(content)
        lines.append("")

    doc_text = "\n".join(lines)
    sidecar = {
        "document_path": output_path,
        "citations": citations,
    }
    return doc_text, sidecar


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sections-file", required=True, help="Path to the section-spec JSON described above")
    ap.add_argument("--held-by", required=True, help="Calling agent name, e.g. draft-report")
    ap.add_argument("--run-id", required=True)
    args = ap.parse_args()

    spec = json.loads(Path(args.sections_file).read_text(encoding="utf-8"))
    doc_text, sidecar = render(spec)

    out_path = ROOT / spec["output_path"]
    sidecar_path = out_path.with_suffix(".evidence.json")
    sidecar["generated_at"] = now_iso()

    schemas, registry = load_registry()
    errors = validate_instance(sidecar, "evidence_sidecar.schema.json", schemas, registry)
    if errors:
        sys.exit("error: generated sidecar failed evidence_sidecar.schema.json -- this is a "
                  "document_assembly.py bug, not a data problem:\n" + "\n".join(f"  - {e}" for e in errors))

    existing_lock = acquire_lock_blocking(out_path, args.held_by, args.run_id, f"document-assembly render {out_path.name}")
    if existing_lock is not None:
        sys.exit(f"error: {out_path} is locked by {existing_lock['held_by']} (run {existing_lock['run_id']}) -- "
                  f"not rendering.")
    try:
        atomic_write_text(out_path, doc_text)
        atomic_write_json(sidecar_path, sidecar)
    finally:
        release_lock(out_path)

    print(f"OK: wrote {out_path} + {sidecar_path} ({len(sidecar['citations'])} citations)")


if __name__ == "__main__":
    main()
