"""A/B the OCR DOCUMENT-level width on a fixed workload of single-page scans.

The page pool is already 24 wide and correctly measured, but it cannot help the
documents that dominate this corpus: 1,170 of 1,797 scanned documents are a
single page, and `min(workers, total)` collapses the page pool to width 1 for
each of them. What is actually in flight is then `doc_workers` alone, which
defaults to 3.

So this measures the document dimension with page count held at 1, which is
both the median and the case where page parallelism is provably unavailable.

Writes nothing: it calls the transcriber directly and discards the text, the
same discipline as bench_classify_workers.py. Re-running OCR through the real
tool would rewrite pages and make the corpus a function of a benchmark.
"""
import io, json, os, sys, time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.abspath('.')
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import ocr_extract
from llm_providers import build_provider, ProviderConfig

N = int(sys.argv[1]) if len(sys.argv) > 1 else 12
WIDTHS = [int(x) for x in (sys.argv[2].split(',') if len(sys.argv) > 2
                           else ['3', '6', '8'])]
OUT = sys.argv[3] if len(sys.argv) > 3 else 'ab_ocr_docs.json'

# Fixed workload: single-page scanned documents whose source PDF still exists.
targets = []
for m in sorted(__import__('glob').glob('outputs/CASE_*/ocr_result_*.json')):
    try:
        d = json.load(io.open(m, encoding='utf-8'))
    except Exception:
        continue
    if d.get('extraction_method') != 'ocr' or len(d.get('pages') or []) != 1:
        continue
    case, doc = d.get('case_id'), d.get('document_id')
    pdf = os.path.join('data', 'raw', case, '%s.pdf' % doc)
    if os.path.exists(pdf):
        targets.append((case, doc, pdf))
    if len(targets) >= N:
        break

print("workload: %d single-page scanned documents" % len(targets), flush=True)
if not targets:
    sys.exit("no usable targets: raw PDFs absent")

provider = build_provider(ProviderConfig('claude-cli'), root=ROOT)


def transcribe(job):
    case, doc, pdf = job
    t0 = time.perf_counter()
    ok = True
    try:
        # resume=False: the per-document cache would turn every repeat width
        # into a cache-hit measurement instead of an OCR measurement.
        ocr_extract.run_ocr(case, doc, Path(pdf), reader_a=provider,
                            resume=False, max_workers=1, single_reader=True,
                            progress=lambda _m: None)
    except Exception as exc:
        ok = False
        print("   error %s %s: %s" % (case, doc, str(exc)[:90]), flush=True)
    return time.perf_counter() - t0, ok


results = {}
for w in WIDTHS:
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=w) as pool:
        rows = list(pool.map(transcribe, targets))
    wall = time.perf_counter() - t0
    per = [r[0] for r in rows]
    results[w] = {
        "workers": w, "wall_s": round(wall, 2),
        "serial_sum_s": round(sum(per), 2),
        "speedup": round(sum(per) / wall, 2),
        "mean_call_s": round(sum(per) / len(per), 2),
        "ok": sum(1 for r in rows if r[1]), "n": len(rows),
    }
    print("  doc_workers=%-3d wall=%7.1fs  speedup=%5.2fx  mean_call=%6.2fs  ok=%d/%d"
          % (w, wall, sum(per) / wall, sum(per) / len(per),
             results[w]["ok"], len(rows)), flush=True)

json.dump({"n": len(targets), "results": results},
          io.open(OUT, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print("wrote %s" % OUT)
