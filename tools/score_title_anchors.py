"""Score the title-anchor rule against every classification the model made.

Zero provider calls: the model's verdicts are already on disk as contracts, so
its answers are the comparison set. What is measured is coverage (how many
documents the rule can settle) and agreement (how often it reaches the model's
answer), plus every disagreement in full so each can be read.

The rule is fed the same thing the pipeline would give it -- the first lines of
the document's own processed text -- not the classifier's evidence quote, which
the model chose after deciding. Using the quote would score a rule that gets to
see the answer.
"""
import io, json, glob, os, sys, collections

sys.path.insert(0, os.path.join(os.path.abspath('.'), 'tools'))
import segment_case as sc

SCAN_LINES = sc._MEDICAL_TITLE_SCAN_LINES


def title_type_from_pages(case, doc):
    """What the rule concludes from the document's own first lines."""
    for rel in (os.path.join('data', 'processed', case, doc, 'page_001.md'),
                os.path.join('data', 'processed', case, doc, 'redacted_text.md')):
        if not os.path.exists(rel):
            continue
        try:
            text = io.open(rel, encoding='utf-8', errors='replace').read()
        except OSError:
            continue
        lines = [l for l in text.splitlines() if l.strip()][:SCAN_LINES]
        hits = []
        for line in lines:
            t = sc.document_type_from_title(line)
            if t:
                hits.append((t, line.strip()))
        if not hits:
            continue
        # Ambiguity is a refusal, not a coin flip: if the header window names
        # two different form kinds, the rule has not identified the document.
        kinds = {t for t, _ in hits}
        if len(kinds) > 1:
            return None, 'ambiguous:' + '/'.join(sorted(kinds))
        return hits[0][0], hits[0][1]
    return None, None


agree = disagree = nomatch = 0
by_type = collections.Counter()
disagreements = []
scanned = 0

for p in sorted(glob.glob('outputs/CASE_*/classification_result_*.json')):
    try:
        d = json.load(io.open(p, encoding='utf-8'))
    except Exception:
        continue
    if (d.get('classification_source') or 'model') != 'model':
        continue
    case = os.path.basename(os.path.dirname(p))
    doc = os.path.basename(p).replace('classification_result_', '').replace('.json', '')
    model_type = d.get('predicted_document_type')
    scanned += 1
    rule_type, evidence = title_type_from_pages(case, doc)
    if rule_type is None:
        nomatch += 1
        continue
    by_type[rule_type] += 1
    if rule_type == model_type:
        agree += 1
    else:
        disagree += 1
        disagreements.append({
            "case": case, "doc": doc, "model": model_type,
            "model_conf": d.get('confidence'),
            "model_label": d.get('document_type_label'),
            "rule": rule_type, "line": evidence,
        })

matched = agree + disagree
print("model-classified documents with processed text: %d" % scanned)
print("  rule settles : %d (%.1f%%)" % (matched, 100 * matched / max(scanned, 1)))
print("    agrees     : %d (%.1f%% of settled)" % (agree, 100 * agree / max(matched, 1)))
print("    disagrees  : %d" % disagree)
print("  rule declines: %d" % nomatch)
print()
print("settled by type:")
for k, n in by_type.most_common():
    print("  %-30s %d" % (k, n))
print()
print("=== every disagreement ===")
for x in disagreements:
    print("%s %s" % (x['case'], x['doc']))
    print("   model: %-24s conf=%-5s label=%s" % (x['model'], x['model_conf'], x['model_label']))
    print("   rule : %-24s from line: %r" % (x['rule'], (x['line'] or '')[:70]))

json.dump({"scanned": scanned, "settled": matched, "agree": agree,
           "disagree": disagree, "declined": nomatch,
           "disagreements": disagreements},
          io.open(sys.argv[1] if len(sys.argv) > 1 else 'anchor_score.json',
                  'w', encoding='utf-8'), ensure_ascii=False, indent=1)
