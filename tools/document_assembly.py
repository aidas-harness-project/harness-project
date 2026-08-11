"""Renders narrative documents (screening_report.md, draft_report_v*.md,
rebuttal_points.md) from agent-provided section content, and auto-generates
the [E#] citation tags plus the matching .evidence.json sidecar in one pass.

See harness-guardrails P1. No agent hand-writes a tag number or edits a
sidecar file directly -- an agent writes `{{E}}` as an inline placeholder
wherever a citation belongs, in the same order as its evidence_references
list for that section; this tool replaces each placeholder with a
sequentially-numbered [E#] tag and writes the sidecar from the same data,
so a tag and its citation can never drift out of sync.

Template enforcement (--template): pass a key from templates/registry.json.
Sections are validated for presence and order against that template's heading
patterns before anything touches disk. A mismatch is a hard exit, with the same
fail/don't-persist contract as sidecar validation. Without --template the tool
renders whatever it is given (correct for rebuttal_points.md, whose per-reason
structure repeats dynamically and has no registry entry on purpose).

This writes into outputs/ like any other DAO write path -- locked
(held-by/run-id, same convention as dao.py write-contract, so dao.py
check-lock correctly sees a render in progress) and atomic. The generated
sidecar is schema-validated against evidence_sidecar.schema.json before
either file touches disk; a failure there is this tool's own bug (the
agent's evidence_references were already well-formed going in), not a data
problem to route around.

Legacy section input (--sections-file), one JSON object:
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

Structured draft input is a schema-valid loss_adjustment_report.v1 object.
The registry supplies deterministic grouping and headings; evidence IDs resolve
to exact document/page/quote citations:

    python tools/document_assembly.py \\
        --structured-report-file /tmp/loss_adjustment_report_v1.json \\
        --template 개인보험_후유장해형 \\
        --output-path outputs/CASE_003/draft_report_v1.md \\
        --held-by draft-report --run-id RUN_20260710_001
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from dao import acquire_lock_blocking, release_lock, atomic_write_text, atomic_write_json, now_iso
from _validation import load_registry, validate_instance

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_REGISTRY = ROOT / "templates" / "registry.json"


_CASE_ID_RE = re.compile(r"(CASE_[0-9]{3})")


def verify_citation_quotes(sidecar: dict, case_id: str) -> list[str]:
    """Every citation's quote must appear verbatim in its document's processed
    text. Returns error strings; empty means every quote resolved.

    This tool renders whatever it is handed. It auto-generates the `[E#]` tags
    and the sidecar from one source so a tag and its citation cannot drift --
    but "the tag matches the sidecar" says nothing about whether the QUOTE is
    real. A citation invented wholesale renders cleanly, passes
    read-evidence-tags (consistent, 0 orphaned, 0 unused), and ships.

    Not hypothetical: writing CASE_909's v2 draft, 29 of 190 citations were
    wrong -- quotes reconstructed from memory, and documents misattributed
    (outpatient bills to DOC_014 instead of DOC_015/016, the 위자료 기준표 to
    DOC_002 instead of DOC_017). They were caught only because that agent
    happened to write its own verifier first. Nothing structural would have
    caught them, which made P1 -- the harness's stated highest-probability
    failure mode -- rest on an agent's initiative.

    Whitespace is normalized on both sides before comparison: extraction
    line-wraps mid-sentence, so a quote spanning a wrap is a real quote and
    failing it would train callers to quote only fragments. Everything else is
    exact -- this establishes the words are on the page, not that they were
    read correctly.
    """
    processed = ROOT / "data" / "processed" / case_id
    errors: list[str] = []
    cache: dict[str, str | None] = {}
    for citation in sidecar.get("citations", []):
        doc_id = citation.get("document_id")
        quote = citation.get("quote")
        tag = citation.get("tag")
        if not doc_id or not quote:
            continue
        if doc_id not in cache:
            path = processed / doc_id / "redacted_text.md"
            try:
                cache[doc_id] = path.read_text(encoding="utf-8")
            except OSError:
                cache[doc_id] = None
        text = cache[doc_id]
        if text is None:
            errors.append(
                f"[{tag}] {doc_id}: no processed text at "
                f"data/processed/{case_id}/{doc_id}/redacted_text.md -- a "
                "citation cannot point at a document this case has not "
                "processed")
            continue
        if quote in text:
            continue
        if _squeeze(quote) and _squeeze(quote) in _squeeze(text):
            continue
        errors.append(
            f"[{tag}] {doc_id} p{citation.get('page')}: quote not found in "
            f"that document's processed text: {quote[:60]!r}")
    return errors


def _squeeze(text: str) -> str:
    return re.sub(r"\s+", "", text)


def validate_template(headings: list[str], template_key: str) -> list[str]:
    """Check section presence + order against templates/registry.json.

    Returns a list of human-readable error strings; empty means the
    document conforms. allow_extra_sections=false (the only mode currently
    used) demands exactly one heading per pattern, in order -- a missing,
    extra, or misplaced section is an error, not a warning."""
    registry = json.loads(TEMPLATE_REGISTRY.read_text(encoding="utf-8"))["templates"]
    if template_key not in registry:
        return [f"unknown template {template_key!r} -- registry has: {', '.join(sorted(registry))}"]
    entry = registry[template_key]
    patterns = entry["heading_patterns"]
    errors = []
    if entry.get("allow_extra_sections", False):
        # ordered-subsequence match: every pattern must hit some heading, in order
        pos = 0
        for pat in patterns:
            while pos < len(headings) and not re.search(pat, headings[pos]):
                pos += 1
            if pos == len(headings):
                errors.append(f"required section matching {pat!r} is missing (or out of order)")
            else:
                pos += 1
        return errors
    if len(headings) != len(patterns):
        errors.append(f"section count mismatch: template {template_key!r} requires exactly "
                      f"{len(patterns)} sections, got {len(headings)}")
    for i, (pat, heading) in enumerate(zip(patterns, headings), start=1):
        if not re.search(pat, heading):
            errors.append(f"section {i}: heading {heading!r} does not match required pattern {pat!r}")
    for extra in headings[len(patterns):]:
        errors.append(f"unexpected extra section: {extra!r}")
    return errors


def sections_from_structured_report(
    document: dict, template_key: str, output_path: str
) -> dict:
    """Convert a validated authoring contract into the existing render spec.

    The template registry owns the heading and grouping map.  Evidence IDs in
    structured statements resolve to exact document/page/quote records here,
    so the existing renderer can keep owning citation tags and source checks.
    """
    templates = json.loads(TEMPLATE_REGISTRY.read_text(encoding="utf-8"))["templates"]
    template = templates.get(template_key)
    if template is None:
        raise ValueError(f"unknown template {template_key!r}")
    profile = document.get("document_profile", {})
    family = profile.get("family")
    if family not in template.get("report_families", []):
        raise ValueError(f"template {template_key!r} does not accept family {family!r}")
    if profile.get("claim_mechanism") not in template.get("claim_mechanisms", []):
        raise ValueError(f"template {template_key!r} does not accept claim mechanism")
    if profile.get("mode") != template.get("mode"):
        raise ValueError(f"template {template_key!r} does not accept report mode")

    evidence = {
        item["evidence_id"]: item for item in document.get("evidence_registry", [])
    }
    sections = []
    headings = template.get("render_headings", [])
    groups = template.get("structured_section_groups", [])
    if len(headings) != len(groups):
        raise ValueError(f"template {template_key!r} has an invalid structured render map")
    for heading, group in zip(headings, groups):
        content_parts = []
        references = []
        if not group:
            content_parts.append(profile.get("title", ""))
        for section_name in group:
            section = document["sections"][section_name]
            if len(group) > 1:
                content_parts.append(f"### {section['heading']}")
            if section["status"] != "included":
                content_parts.append(section["rationale"])
                continue
            for statement in section["statements"]:
                evidence_ids = statement["evidence_refs"]
                content_parts.append(
                    statement["text"] + " " + " ".join("{{E}}" for _ in evidence_ids)
                )
                for evidence_id in evidence_ids:
                    source = evidence[evidence_id]
                    reference = {
                        "document_id": source["document_id"],
                        "quote": source["quote"],
                    }
                    if source.get("page") is not None:
                        reference["page"] = source["page"]
                    references.append(reference)
        sections.append(
            {
                "heading": heading,
                "content": "\n\n".join(content_parts),
                "evidence_references": references,
            }
        )
    return {"output_path": output_path, "sections": sections}


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
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--sections-file", help="Path to the section-spec JSON described above")
    source.add_argument(
        "--structured-report-file",
        help="Path to a validated loss_adjustment_report_vN.json contract",
    )
    ap.add_argument(
        "--output-path",
        help="Required with --structured-report-file; relative narrative path under outputs/",
    )
    ap.add_argument("--held-by", required=True, help="Calling agent name, e.g. draft-report")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--template", help="Key in templates/registry.json to enforce section "
                    "presence/order against (e.g. 진단수술비형, screening_report). Omit only for "
                    "documents with no registry entry (rebuttal_points).")
    args = ap.parse_args()

    if args.structured_report_file:
        if not args.template:
            sys.exit("error: --structured-report-file requires --template")
        if not args.output_path:
            sys.exit("error: --structured-report-file requires --output-path")
        document = json.loads(
            Path(args.structured_report_file).read_text(encoding="utf-8")
        )
        schemas, registry = load_registry()
        report_errors = validate_instance(
            document, "loss_adjustment_report.schema.json", schemas, registry
        )
        if report_errors:
            sys.exit(
                "error: structured report failed loss_adjustment_report.schema.json "
                "-- nothing written:\n"
                + "\n".join(f"  - {error}" for error in report_errors)
            )
        output_case = _CASE_ID_RE.search(args.output_path)
        report_case = document.get("case_reference", {}).get("case_id")
        if output_case and output_case.group(1) != report_case:
            sys.exit(
                "error: structured report case_reference.case_id does not match "
                "the output_path case -- nothing written"
            )
        try:
            spec = sections_from_structured_report(
                document, args.template, args.output_path
            )
        except ValueError as exc:
            sys.exit(f"error: {exc} -- nothing written")
    else:
        if args.output_path:
            sys.exit("error: --output-path is only valid with --structured-report-file")
        spec = json.loads(Path(args.sections_file).read_text(encoding="utf-8"))

    if args.template:
        headings = [s["heading"] for s in spec["sections"]]
        template_errors = validate_template(headings, args.template)
        if template_errors:
            sys.exit(f"error: sections do not conform to template {args.template!r} "
                     "(templates/registry.json) -- nothing written:\n"
                     + "\n".join(f"  - {e}" for e in template_errors))

    doc_text, sidecar = render(spec)

    # Containment (fleet F8): output_path must stay under outputs/. An absolute
    # or ../ path would otherwise escape (Path('/x') / abs discards ROOT), letting
    # a spec write anywhere on disk.
    rel = spec["output_path"]
    if Path(rel).is_absolute() or ".." in Path(rel).parts:
        sys.exit(f"error: output_path {rel!r} must be a relative path under outputs/")
    out_path = (ROOT / rel).resolve()
    outputs_root = (ROOT / "outputs").resolve()
    if outputs_root != out_path and outputs_root not in out_path.parents:
        sys.exit(f"error: output_path {rel!r} escapes outputs/ -- refusing")
    sidecar_path = out_path.with_suffix(".evidence.json")
    sidecar["generated_at"] = now_iso()

    # P1's deterministic floor, and it runs BEFORE anything is written -- the
    # same fail/don't-persist contract as the template and sidecar checks. A
    # document whose citations do not resolve must not reach disk, because
    # every checker downstream (read-evidence-tags, check-untagged-claims)
    # reads the tag layer and would report it clean.
    case_match = _CASE_ID_RE.search(rel)
    if case_match:
        quote_errors = verify_citation_quotes(sidecar, case_match.group(1))
        if quote_errors:
            sys.exit(
                "error: citation quotes do not appear in the processed source "
                "-- nothing written:\n"
                + "\n".join(f"  - {e}" for e in quote_errors)
                + "\n  Quote from data/processed/<CASE>/<DOC>/redacted_text.md, "
                  "do not reconstruct from memory. Whitespace differences are "
                  "tolerated; wrong words and wrong documents are not.")
    else:
        print(f"WARNING: output_path {rel!r} carries no CASE_NNN, so citation "
              "quotes could not be verified against a processed source.",
              file=sys.stderr)

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
