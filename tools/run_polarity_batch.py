"""Parallel driver for `dao.py analyze-policy-polarity`.

Why this exists: stage 4 normalization is dominated by the semantic polarity
analyzer, measured at ~89s per receipt on CASE_907/DOC_003. The conditions in a
policy document are independent of one another -- Phase A reads only the source
passage and Phase B only the passage plus the extracted condition -- so nothing
about the analysis requires them to run one at a time. Sequentially, DOC_003's
several hundred conditions come to 10+ hours; at 10 workers that is about an
hour, with no change to how any individual verdict is reached.

Concurrency is safe by the DAO's own design, not by an assumption made here.
`cmd_analyze_policy_polarity` deliberately runs the provider with **no shared
file lock held** and only acquires the index lock to commit. At commit it
re-derives the source snapshot and refuses a candidate whose provenance moved
while the provider was running, and it treats a receipt another process
committed for the same `cache_key` as success ("concurrent semantic analysis
already committed ... candidate discarded"). The lock itself blocks rather than
failing, so overlapping commits serialize instead of colliding. This driver
therefore adds parallelism without weakening any check: every receipt is still
produced by the same two-phase, bucket-blind analysis and validated identically.

What it deliberately does NOT do: write `normalized_policy_clause_*.json` or
any other contract. `write-contract` replaces a whole file under a lock, so
concurrent writers to one document's clause file would clobber each other.
Receipts are the parallel-safe unit (append-only, cache-keyed, conflict-aware),
so this driver pre-warms the receipt cache and leaves contract assembly to the
policy-pipeline agent, which then finds every `analyze-policy-polarity` call it
makes already answered -- `PASS: semantic analysis cache hit ... (provider not
called)` -- and spends no provider time at all.

Two settings matter for a real batch and are documented at their definitions:
`HARNESS_LOCK_POLL_INTERVAL_SECONDS` (the DAO's 30s human-gate poll cadence is
wrong for overlapping millisecond commits -- ten workers spent 4.5 minutes
polling for 10 cache hits on CASE_907, 0.1 minutes at 0.3s) and
`HARNESS_LLM_COMPARE_TIMEOUT_SECONDS` (60s was tuned for a short OCR verdict;
a long Korean article legitimately runs past it).

Grouping conditions by 약관 (`--group`) does not change what is computed; it
only controls the order work is dispatched in and how progress is reported, so
a long run can be read as "which 약관 are done" rather than an opaque count.

Input: a JSON file holding a list of condition jobs.

    [
      {
        "group": "구내치료비 추가특별약관 (p5-6)",
        "doc_id": "DOC_003",
        "bucket": "payout_conditions",
        "condition_text": "...",
        "source_span_uids": ["PS-..."],
        "source_selector": {"source_spans": [{"page": 5, "start_char": 0,
                                              "end_char": 87, "quote": "..."}]}
      }
    ]

`group` is optional and free-form. Everything else mirrors the flags of
`dao.py analyze-policy-polarity` one-for-one.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DAO = ROOT / "tools" / "dao.py"

# A cache hit prints this and costs no provider time; worth counting separately
# so a re-run's speedup is visible rather than looking like the analyzer got
# mysteriously faster.
CACHE_HIT_MARKER = "cache hit"
CONCURRENT_MARKER = "concurrent semantic analysis already committed"

_print_lock = threading.Lock()


def _log(message: str) -> None:
    with _print_lock:
        print(message, flush=True)


# The analyzer's own self-consistency checks reject a response that contradicts
# itself (a settled `affirmative` verdict alongside a restrictive proposition, a
# `mixed` reading that forgot review_required, an empty proposition list). These
# are the validation working, not a rule to loosen -- and they are also
# non-deterministic, so the same passage usually passes on a second sampling.
# Retried rather than relaxed; a condition that fails every attempt is reported
# for a human to look at.
_RETRYABLE_MARKERS = (
    "propositions must be a non-empty list",
    "must set review_required=true",
    "settled source classification conflicts",
    "must identify an ambiguous proposition",
    "timed out after",
)


def _retryable(output: str) -> bool:
    return any(marker in output for marker in _RETRYABLE_MARKERS)


def _invoke(job: dict, case_id: str, held_by: str, run_id: str,
            timeout: int) -> tuple[bool, str]:
    argv = [
        sys.executable, str(DAO), "analyze-policy-polarity", case_id,
        "--doc-id", job["doc_id"],
        "--source-selector-json", json.dumps(job["source_selector"],
                                             ensure_ascii=False),
        "--condition-text", job["condition_text"],
        "--bucket", job["bucket"],
        "--held-by", held_by,
        "--run-id", run_id,
    ]
    for uid in job["source_span_uids"]:
        argv += ["--source-span-uid", uid]

    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              cwd=str(ROOT), timeout=timeout)
        return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return False, f"timeout after {timeout}s"


def run_one(job: dict, case_id: str, held_by: str, run_id: str,
            timeout: int, attempts: int = 3) -> dict:
    """One condition, retried on a self-inconsistent provider response. Never
    raises: a failed job is reported as data so one bad condition cannot abort
    the batch."""
    started = time.time()
    for attempt in range(1, attempts + 1):
        ok, out = _invoke(job, case_id, held_by, run_id, timeout)
        if ok or attempt == attempts or not _retryable(out):
            break

    return {
        "group": job.get("group", ""),
        "bucket": job["bucket"],
        "ok": ok,
        "attempts": attempt,
        "cached": CACHE_HIT_MARKER in out or CONCURRENT_MARKER in out,
        "elapsed": time.time() - started,
        "output": out.strip()[-400:],
        "condition_preview": job["condition_text"][:60],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("case_id")
    ap.add_argument("jobs_file", help="JSON list of condition jobs")
    ap.add_argument("--workers", type=int, default=10,
                    help="concurrent analyses (default 10)")
    ap.add_argument("--held-by", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--timeout", type=int, default=600,
                    help="per-analysis timeout in seconds (default 600)")
    ap.add_argument("--group", action="store_true",
                    help="dispatch grouped by 'group' and report per group")
    args = ap.parse_args()

    jobs = json.loads(Path(args.jobs_file).read_text(encoding="utf-8"))
    if not isinstance(jobs, list) or not jobs:
        sys.exit("error: jobs file must be a non-empty JSON list")

    required = {"doc_id", "bucket", "condition_text", "source_span_uids",
                "source_selector"}
    for index, job in enumerate(jobs):
        missing = required - set(job)
        if missing:
            sys.exit(f"error: job {index} is missing {sorted(missing)}")

    if args.group:
        # Stable order: whole 약관 finish together, so partial progress is
        # readable as completed units rather than a scatter of conditions.
        jobs = sorted(jobs, key=lambda j: j.get("group", ""))

    total = len(jobs)
    _log(f"{total} condition(s), {args.workers} worker(s), case {args.case_id}")

    done = 0
    results: list[dict] = []
    started = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(run_one, job, args.case_id, args.held_by,
                        args.run_id, args.timeout): job
            for job in jobs
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            done += 1
            mark = "cached" if result["cached"] else ("ok" if result["ok"] else "FAIL")
            elapsed = time.time() - started
            rate = elapsed / done
            retried = f" x{result['attempts']}" if result.get("attempts", 1) > 1 else ""
            _log(f"  [{done}/{total}] {mark:6s} {result['elapsed']:5.0f}s{retried:3s}  "
                 f"{result['group'][:34]:34s} eta={(total - done) * rate / 60:.0f}m")
            if not result["ok"]:
                _log(f"          {result['output'][:200]}")

    elapsed = time.time() - started
    ok = sum(1 for r in results if r["ok"])
    cached = sum(1 for r in results if r["cached"])
    failed = [r for r in results if not r["ok"]]

    _log("")
    _log(f"TOTAL {elapsed / 60:.1f}m | ok={ok} cached={cached} failed={len(failed)}")
    if ok - cached:
        provider_time = sum(r["elapsed"] for r in results if r["ok"] and not r["cached"])
        _log(f"  provider calls: {ok - cached}, "
             f"mean {provider_time / (ok - cached):.0f}s each")
        _log(f"  wall-clock speedup vs sequential: "
             f"{sum(r['elapsed'] for r in results) / elapsed:.1f}x")

    if args.group:
        groups: dict[str, list[dict]] = {}
        for r in results:
            groups.setdefault(r["group"] or "(ungrouped)", []).append(r)
        _log("")
        _log("per 약관:")
        for name in sorted(groups):
            items = groups[name]
            bad = sum(1 for r in items if not r["ok"])
            _log(f"  {'FAIL' if bad else ' ok '} {name[:50]:50s} "
                 f"{len(items):3d} condition(s)" + (f", {bad} failed" if bad else ""))

    if failed:
        _log("")
        _log(f"{len(failed)} failure(s):")
        for r in failed[:15]:
            _log(f"  [{r['bucket']}] {r['condition_preview']}")
            _log(f"    {r['output'][:240]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
