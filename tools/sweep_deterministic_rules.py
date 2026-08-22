"""Fire the deterministic rules across every processed page in the corpus.

The risk a unit test cannot cover: a pattern matching somewhere it should not
-- a 소견서 quoting a rate in prose, an insurer letter restating one, a policy
clause defining how a rate is calculated. Those would become asserted values
with a citation, which is worse than a missed field.

Reads data/processed/CASE_*/DOC_*/redacted_text.md, which is the same text the
DAO serves to the analysis stages, and pairs each hit with the document_type
recorded in that case's document_manifest.json.
"""
import sys, glob, io, json, collections, os, re

sys.path.insert(0, 'tools')
import claim_analysis_deterministic as det

FIELDS = [{"field_id": f} for f in det.RULES]
hits = collections.Counter()
examples = collections.defaultdict(list)
scanned = 0

# document_type per (case, doc), from each manifest.
types = {}
for mpath in glob.glob(os.path.join('outputs', 'CASE_*', 'document_manifest.json')):
    case = mpath.replace('\\', '/').split('/')[1]
    try:
        m = json.load(io.open(mpath, encoding='utf-8'))
    except Exception:
        continue
    for doc in m.get('documents') or []:
        types[(case, doc.get('document_id'))] = doc.get('document_type')

PAGE_RE = re.compile(r'<<<PAGE page=(\d+)>>>')

for path in glob.glob(os.path.join('data', 'processed', 'CASE_*', 'DOC_*',
                                   'redacted_text.md')):
    parts = path.replace('\\', '/').split('/')
    case, doc = parts[2], parts[3]
    try:
        text = io.open(path, encoding='utf-8', errors='replace').read()
    except Exception:
        continue
    # Split on the page markers the pipeline embeds; fall back to one page.
    pieces = PAGE_RE.split(text)
    pages = []
    if len(pieces) > 1:
        for i in range(1, len(pieces), 2):
            pages.append({"page": int(pieces[i]), "text": pieces[i + 1]})
    else:
        pages = [{"page": 1, "text": text}]
    scanned += 1
    found = det.extract(pages, FIELDS)
    if not found:
        continue
    dtype = types.get((case, doc)) or 'unknown'
    hits[dtype] += 1
    examples[dtype].append((case, doc,
                            {k: v['value'] for k, v in found.items()}))

print("scanned %d processed documents" % scanned)
print()
print("documents where at least one rule fired, by document_type:")
for k, n in hits.most_common():
    print("  %-34s %d" % (k, n))
    for ex in examples[k][:8]:
        print("        %s %s %s" % ex)
