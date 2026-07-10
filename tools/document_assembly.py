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

Input (--sections-file), one JSON object:
{
  "output_path": "outputs/CASE_003/draft_report_v1.md",
  "sections": [
    {"heading": "1. Case overview", "content": "...text with {{E}} placeholders...",
     "evidence_references": [{"document_id": "DOC_001", "page": 1, "quote": "..."}]}
  ]
}

Usage:
    python tools/document_assembly.py --sections-file /tmp/sections.json
"""
import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
KST = timezone(timedelta(hours=9))


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
            citations.append({
                "tag": tag,
                "document_id": ref["document_id"],
                "page": ref.get("page"),
                "quote": ref["quote"],
            })
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
    args = ap.parse_args()

    spec = json.loads(Path(args.sections_file).read_text(encoding="utf-8"))
    doc_text, sidecar = render(spec)

    out_path = ROOT / spec["output_path"]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(doc_text, encoding="utf-8")

    sidecar_path = out_path.with_suffix(".evidence.json")
    sidecar["generated_at"] = datetime.now(KST).isoformat()
    sidecar_path.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"OK: wrote {out_path} + {sidecar_path} ({len(sidecar['citations'])} citations)")


if __name__ == "__main__":
    main()
