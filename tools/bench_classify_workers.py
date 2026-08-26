"""A/B the classification worker count on one fixed workload.

Measures the pool, not the pipeline: the same N documents are classified at
each width, so the only thing that changes is how many calls are in flight.

Deliberately does NOT write anything. `run_checkpoint1.py classify-only` would
patch the manifest, so repeating it at three widths would rewrite the same
documents three times and make the corpus a function of a benchmark. Instead it
calls the classifier the same way that tool does and throws the answer away --
the provider round trip is what is being timed, and it is identical either way.
"""
import io, json, os, sys, time, collections
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.abspath('.')
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import run_checkpoint1 as rc
from llm_providers import build_provider, ProviderConfig

N = int(sys.argv[1]) if len(sys.argv) > 1 else 24
WIDTHS = [int(x) for x in (sys.argv[2].split(',') if len(sys.argv) > 2
                           else ['1', '4', '8', '12'])]
OUT = sys.argv[3] if len(sys.argv) > 3 else 'ab_workers.json'

# One fixed workload: the first N documents that have processed text.
targets = []
for m in sorted(__import__('glob').glob('outputs/CASE_*/document_manifest.json')):
    case = os.path.basename(os.path.dirname(m))
    try:
        man = json.load(io.open(m, encoding='utf-8'))
    except Exception:
        continue
    for d in man.get('documents') or []:
        doc = d.get('document_id')
        p = os.path.join('data', 'processed', case, doc, 'page_001.md')
        if os.path.exists(p) and d.get('document_type'):
            targets.append((case, doc, p))
        if len(targets) >= N:
            break
    if len(targets) >= N:
        break

print("workload: %d documents" % len(targets), flush=True)

texts = {}
for case, doc, p in targets:
    texts[(case, doc)] = io.open(p, encoding='utf-8', errors='replace').read()[:6000]

provider = build_provider(ProviderConfig('claude-cli'), root=ROOT)
routing = rc._medical_routing.load_routing_config()


def classify(job):
    case, doc, _ = job
    t0 = time.perf_counter()
    try:
        rc.classify_document(texts[(case, doc)], classifier=provider,
                             routing_config=routing)
        ok = True
    except Exception as exc:
        ok = False
        print("   error %s %s: %s" % (case, doc, str(exc)[:80]), flush=True)
    return time.perf_counter() - t0, ok


results = {}
for w in WIDTHS:
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=w) as pool:
        rows = list(pool.map(classify, targets))
    wall = time.perf_counter() - t0
    per = [r[0] for r in rows]
    ok = sum(1 for r in rows if r[1])
    results[w] = {
        "workers": w, "wall_s": round(wall, 2),
        "serial_sum_s": round(sum(per), 2),
        "speedup": round(sum(per) / wall, 2),
        "mean_call_s": round(sum(per) / len(per), 2),
        "ok": ok, "n": len(rows),
    }
    print("  workers=%-3d wall=%7.1fs  speedup=%5.2fx  mean_call=%5.2fs  ok=%d/%d"
          % (w, wall, sum(per) / wall, sum(per) / len(per), ok, len(rows)),
          flush=True)

json.dump({"n": len(targets), "results": results},
          io.open(OUT, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print("wrote %s" % OUT)
