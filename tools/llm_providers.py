"""Provider abstraction for LLM-backed pipeline calls.

This module is deliberately thin: callers own prompt construction and
domain-specific parsing, while providers own how a prompt reaches an
execution backend. That lets OCR, classification, and intake checks share
one provider-selection path without coupling their safety rules to any one
CLI or API surface.

The default backend is ``openrouter``: one OpenAI-compatible HTTP transport
that reaches every model family the harness has run behind a CLI, without a
node subprocess per call. The CLI providers remain selectable -- ``--provider
claude-cli`` / ``codex-cli`` still work unchanged, and the P8 reader pair is
still configured per reader -- but nothing defaults to them any more.

Providers here are all LLM-backed (CLI or HTTP API). An earlier revision
also shipped an offline Tesseract/Ollama trio (``local-ocr``/``local-vlm``/
``local-llm``); it was removed because the pinned local models never
transcribed real Korean claim pages and the runtime was single-machine
(Windows/E:-drive) only -- see the PR #8 review and open-decisions.md #3/#4.
A genuinely technology-independent P8 reader (a real OCR engine) is deferred,
not replaced by these.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import functools
import http.client
import json
import mimetypes
import os
import random
import re
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

# Local tools/trace.py, not the stdlib `trace` -- tools/ precedes stdlib on
# sys.path for every entry point in this repo. Aliased so the shadowing is
# visible at each use site rather than only here.
import trace as trace_mod


ROOT = Path(__file__).resolve().parent.parent

SUPPORTED_PROVIDERS = (
    "openrouter", "claude-cli", "codex-cli", "anthropic-api", "openai-api", "fixture",
)
DEFAULT_PROVIDER = "openrouter"
DEFAULT_ENV_PREFIX = "HARNESS_LLM"

# claude-cli transient-failure retry. Applies only to subprocess-level failures
# (non-zero exit / timeout), never to content agreement -- see
# ClaudeCliProvider._run.
#
# The wait is exponential with FULL JITTER: attempt n sleeps a uniform random
# value in [0, min(cap, base * 2**(n-1))]. Both halves are load-bearing and
# neither works alone.
#
#   * Exponential: a rate limit is a "wait and it clears" condition, so a
#     failing call must back off rather than re-knock at a constant rate. The
#     previous fixed 2.0s gave a total of 4s of waiting across the two gaps,
#     which is shorter than a typical limit window -- all three attempts could
#     burn inside one window and the page would die.
#   * Jitter: page-level concurrency means N calls are in flight at once, so a
#     shared limit fails them at nearly the same instant. With a fixed (or
#     un-jittered exponential) wait, all N re-arrive TOGETHER -- a thundering
#     herd against the exact limit that just rejected them. Randomizing spreads
#     the retries out; measured on 8 simulated workers, arrival spread goes
#     from 0.00s to ~1-3s.
#
# The herd cost grows with worker count, which is why DEFAULT_OCR_WORKERS could
# not safely be raised before this existed (see
# docs/run-notes/OCR_PARALLEL_20260810_001.md).
# NOTE (2026-08-10, T4d): these three are no longer claude-only --
# CodexCliProvider._run now uses the identical curve. The names are kept
# because tests/test_llm_provider_backoff.py and tests/test_llm_providers.py
# reference them, and renaming a shared constant to fix a naming nit is a
# wider change than the retry work itself warrants. Read them as "the CLI
# provider retry policy".
_CLAUDE_CLI_MAX_ATTEMPTS = 3
_CLAUDE_CLI_RETRY_BASE_SECONDS = 2.0
_CLAUDE_CLI_RETRY_CAP_SECONDS = 30.0

# A server that says WHEN to come back is more authoritative than any local
# backoff curve, so a parsed Retry-After wins -- but it is still clamped to the
# cap, since the value arrives from outside and an absurd one must not hang a
# 12-page document behind a single call.
_RETRY_AFTER_PATTERN = re.compile(
    r"retry[-\s_]?after[\"'\s:=]+(\d+(?:\.\d+)?)", re.IGNORECASE
)
# Rate-limit / overload signatures. Matched against the CLI's own diagnostic
# text because a subprocess gives us no status code -- the child prints the
# server's complaint and exits non-zero.
_RATE_LIMIT_PATTERN = re.compile(
    r"\b429\b|rate[-\s_]?limit|too many requests|overloaded|"
    r"\b529\b|quota exceeded|capacity",
    re.IGNORECASE,
)


def _parse_retry_after(text: str) -> float | None:
    """Seconds the server asked us to wait, if it said so at all.

    Only the delta-seconds form is read. HTTP also allows an absolute date, but
    the CLI surfaces server errors as free text rather than headers, so a date
    would be both rare and ambiguous to parse out of prose -- returning None
    falls back to the ordinary backoff, which is the safe direction.
    """
    if not text:
        return None
    match = _RETRY_AFTER_PATTERN.search(text)
    if match is None:
        return None
    try:
        value = float(match.group(1))
    except ValueError:  # pragma: no cover -- regex guarantees a number
        return None
    if value < 0:
        return None
    return min(value, _CLAUDE_CLI_RETRY_CAP_SECONDS)


def _is_rate_limited(text: str) -> bool:
    """Whether a failure diagnostic looks like a rate limit / overload."""
    return bool(text) and _RATE_LIMIT_PATTERN.search(text) is not None


def _retry_delay(attempt: int, detail: str = "") -> float:
    """Seconds to wait before retrying, after `attempt` failed attempts (1-based).

    Full jitter over an exponentially growing ceiling; a server-supplied
    Retry-After replaces the ceiling when present. The floor is deliberately 0
    rather than some minimum -- spreading arrivals is the point, and a call that
    happens to retry immediately is exactly one call, not a herd.
    """
    ceiling = min(
        _CLAUDE_CLI_RETRY_CAP_SECONDS,
        _CLAUDE_CLI_RETRY_BASE_SECONDS * (2 ** max(0, attempt - 1)),
    )
    asked = _parse_retry_after(detail)
    if asked is not None:
        # Honour the server's number as a floor on the wait -- jitter still
        # applies ON TOP so N held-back callers do not resume in lockstep.
        return asked + random.uniform(0, min(_CLAUDE_CLI_RETRY_BASE_SECONDS, ceiling))
    return random.uniform(0, ceiling)

# `compare_text` default. 60s was tuned for the short OCR agreement verdict
# (ocr_extract.compare); the policy-polarity path reuses this same method for a
# two-phase semantic reading of a long Korean article and legitimately runs
# past it, so every attempt times out and no receipt can ever be issued for the
# longer passages. Overridable per deployment rather than raised outright, so
# the fast path stays fast and a slow analyzer is a config change, not a patch.
_COMPARE_TEXT_DEFAULT_TIMEOUT_SECONDS = 60
_COMPARE_TEXT_TIMEOUT_ENV = "HARNESS_LLM_COMPARE_TIMEOUT_SECONDS"


def compare_text_timeout(env: Mapping[str, str] | None = None) -> int:
    """Resolve compare_text's subprocess timeout.

    Invalid or non-positive values fall back to the default rather than
    raising: a malformed timeout must not turn every semantic analysis into a
    hard configuration failure.
    """
    source = os.environ if env is None else env
    raw = str(source.get(_COMPARE_TEXT_TIMEOUT_ENV, "")).strip()
    if not raw:
        return _COMPARE_TEXT_DEFAULT_TIMEOUT_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return _COMPARE_TEXT_DEFAULT_TIMEOUT_SECONDS
    return value if value > 0 else _COMPARE_TEXT_DEFAULT_TIMEOUT_SECONDS


# analyze_text_structured is NOT compare_text, and sharing compare's 60s budget
# was a real defect: V4a's claim-analysis checkpoint-1 driver timed out on 3 of
# 17 documents because extracting facts from a full document is a longer job
# than deciding whether two OCR transcriptions agree. 180s matches the budget
# the image-analysis paths already use for comparable per-document work.
# Deliberately a SEPARATE resolver and env var: raising the OCR comparison
# timeout to fix fact extraction would slow every P8 disagreement path too.
_STRUCTURED_TEXT_DEFAULT_TIMEOUT_SECONDS = 180
_STRUCTURED_TEXT_TIMEOUT_ENV = "HARNESS_STRUCTURED_TEXT_TIMEOUT"
# An upper bound on the accepted override. Without it a fat-fingered value
# (a millisecond figure pasted into a seconds field) becomes a child that
# effectively never times out, which is indistinguishable from a hang.
_STRUCTURED_TEXT_MAX_TIMEOUT_SECONDS = 3600


def structured_text_timeout(env: Mapping[str, str] | None = None) -> int:
    """Resolve analyze_text_structured's subprocess timeout.

    Same fail-safe contract as compare_text_timeout -- malformed, zero, and
    negative values fall back to the default rather than raising, so a bad
    config value cannot turn every structured analysis into a hard failure.
    Values above _STRUCTURED_TEXT_MAX_TIMEOUT_SECONDS also fall back, since an
    implausibly large timeout hides a hang instead of surfacing it.
    """
    source = os.environ if env is None else env
    raw = str(source.get(_STRUCTURED_TEXT_TIMEOUT_ENV, "")).strip()
    if not raw:
        return _STRUCTURED_TEXT_DEFAULT_TIMEOUT_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return _STRUCTURED_TEXT_DEFAULT_TIMEOUT_SECONDS
    if value <= 0 or value > _STRUCTURED_TEXT_MAX_TIMEOUT_SECONDS:
        return _STRUCTURED_TEXT_DEFAULT_TIMEOUT_SECONDS
    return value
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"
_ANTHROPIC_MAX_TOKENS_ENV = "HARNESS_ANTHROPIC_MAX_TOKENS"
_ANTHROPIC_MAX_TOKENS_DEFAULT = 16_000

# ---------------------------------------------------------------- OpenRouter --
#
# OpenRouter is an OpenAI-chat-completions-compatible aggregator, so ONE HTTP
# transport reaches every model family the harness has used behind a CLI
# (Anthropic, OpenAI, Google, open weights) without a per-family provider class.
# That is why it is the default: the CLI providers pay a full node process per
# call and can only reach their own vendor.
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# Sent on every call. Checked against the live model catalogue on 2026-08-22:
# of 420 models, 40 declare a `top_provider.max_completion_tokens` BELOW this
# default (as low as 2,048 -- gemma-2-27b-it, ui-tars-1.5-7b, the cohere
# command-r family at 4,000, gpt-4-turbo at 4,096), and 11 do not list
# `max_tokens` among their supported parameters at all. Pointing the harness at
# one of those needs this lowered; leaving it high risks an upstream 400 that
# reads as a model-name problem. It is not raised to a per-model lookup here
# because that would add a second network call before every completion.
_OPENROUTER_MAX_TOKENS_ENV = "HARNESS_OPENROUTER_MAX_TOKENS"
_OPENROUTER_MAX_TOKENS_DEFAULT = 16_000

# Optional attribution headers OpenRouter reads for its public leaderboards.
# Both are opt-in: unset means the call is simply unattributed, never rejected.
_OPENROUTER_REFERER_ENV = "HARNESS_OPENROUTER_REFERER"
_OPENROUTER_TITLE_ENV = "HARNESS_OPENROUTER_TITLE"

# PII posture. Case material is Korean insurance-claim content, and OCR sends
# page images BEFORE redaction -- there is no earlier point at which a page can
# be read. OpenRouter's default routing (`data_collection: "allow"`) permits
# providers that RETAIN prompts, including for training; the CLI transports this
# replaces reached one vendor under that vendor's API terms and had no such
# fan-out. So the default here is `deny`, which restricts routing to providers
# that do not collect prompt data. It narrows the eligible provider pool and can
# make a model unroutable -- that is the intended direction for this corpus, and
# `HARNESS_OPENROUTER_DATA_COLLECTION=allow` is the deliberate, recorded opt-out.
# `HARNESS_OPENROUTER_ZDR=1` additionally pins routing to zero-data-retention
# endpoints. Neither replaces open-decisions.md #3: every one of these providers
# is still an external service receiving the page.
_OPENROUTER_DATA_COLLECTION_ENV = "HARNESS_OPENROUTER_DATA_COLLECTION"
_OPENROUTER_DATA_COLLECTION_DEFAULT = "deny"
_OPENROUTER_DATA_COLLECTION_VALUES = ("deny", "allow")
_OPENROUTER_ZDR_ENV = "HARNESS_OPENROUTER_ZDR"

# A base URL is where the Bearer key is sent. Plain http would put it, and the
# case material, on the wire in clear. Refused unless the host is loopback (the
# test server, a local proxy on the same machine).
_OPENROUTER_INSECURE_OPT_OUT_ENV = "HARNESS_OPENROUTER_ALLOW_INSECURE_BASE_URL"
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})

# Server-supplied diagnostics are echoed into the exception message, and a 4xx
# body can quote back part of the request -- which is a page of claim text.
# Enough to diagnose, not enough to spill a document into a console or CI log.
_OPENROUTER_ERROR_BODY_MAX_CHARS = 2_000

# urllib would otherwise send `Python-urllib/3.x`, which edge protection in
# front of a public API is entitled to challenge or block. Naming the client
# costs nothing and removes a failure mode that would look like an
# authentication problem. Overridable for a deployment that must identify
# itself differently.
_OPENROUTER_USER_AGENT_ENV = "HARNESS_OPENROUTER_USER_AGENT"
_OPENROUTER_DEFAULT_USER_AGENT = "loss-adjustment-harness/0.1 (+tools/llm_providers.py)"

# Transport-level retry. Distinct from the CLI providers' retry in one way that
# matters: a subprocess only reports "non-zero exit", so ClaudeCliProvider
# cannot tell a rate limit from a schema rejection and therefore refuses to
# retry a structured call at all (that would hide a whole extra model round
# behind the caller's single P4 correction). HTTP gives us a status code, so
# retries here are restricted to statuses where the request provably did NOT
# produce a completion -- no model output is ever discarded and re-rolled, and
# structured calls retry on exactly the same narrow set as plain ones.
_OPENROUTER_MAX_ATTEMPTS = 3
_OPENROUTER_RETRY_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})

# finish_reason values that mean the text in hand is not a complete answer.
# "length" is the truncation case the other HTTP providers already refuse
# (anthropic stop_reason=max_tokens, openai status=incomplete); "error" is
# OpenRouter's own mid-generation provider failure; "content_filter" produced
# no usable content by definition.
_OPENROUTER_BAD_FINISH_REASONS = frozenset({"length", "error", "content_filter"})

# ---------------------------------------------------------------- T6: in-flight cap --
#
# A process-wide ceiling on concurrent provider calls. It exists because the
# pools nest: page workers already run 4-8 wide, and document-level workers
# (T8) multiply that -- 2 documents x 8 pages is 16 concurrent CLI children,
# each a full node process, against a shared rate limit.
#
# PROCESS-wide is sufficient, and only became sufficient once T4a removed
# redaction's per-page dao subprocess: a cross-PROCESS ceiling would need a
# file lock, which is exactly the 30s-poll cost this layer exists to avoid.
#
# Acquired around the LEAF call only -- the single subprocess.run/urlopen --
# never around a whole P8 chain. reader_a -> reader_b -> compare runs inside
# one page worker: if that worker held a permit for the whole chain while its
# own leaf calls waited for permits, it would deadlock against itself. The
# permit is also released across a retry backoff sleep, so a retrying call
# does not hold capacity it is not using.
#
# Raised 6 -> 16 on 2026-08-11, on measurement rather than on headroom. 6 was
# a guess made before anything nested; measured on CASE_953 (5 documents, 6
# pages each, 8 page workers, 2 document workers) it was the binding
# constraint and it was eating the gain it was meant to protect:
#
#   cap  wall     per-document time SUM   observed peak
#    6   125.9s   214.4s  (+59.8s)        6/6   <- pinned
#   16    98.1s   156.4s  (+1.8s)         12/16 <- slack
#
# With the cap at 6, running documents concurrently made each document
# individually slower (DOC_003: 53.5s -> 87.9s) because 16 requests were
# squeezed through 6 slots -- the parallel win was handed straight back as
# queueing. At 16 that overhead is essentially gone (60s -> 1.8s).
#
# UNCAPPED between 2026-08-11 and 2026-08-12, then RE-ARMED at 12 on
# measurement. The uncapping was reasoned from the full CASE_953 run: 99
# provider calls, ZERO `provider.queue` spans, zero rate-limit errors, so "a
# limiter that never engages is not protecting anything". Both observations
# were true and still are -- CASE_911 reproduced them exactly (97 calls, 0
# queue spans, 0 errors).
#
# What that reasoning missed is that it only ever watched for ONE failure
# mode. It concluded the backend was not rate-limiting -- correct -- and from
# there that no ceiling was needed, which does not follow. Concurrency on a
# CLI provider is also bounded LOCALLY: every call is a full node child
# process, and past a point the machine spends more on spawning them than the
# added parallelism returns. That shows up as slowness, never as an error, so
# the "zero rate-limit failures" evidence is blind to it by construction.
#
# Measured 2026-08-12 on TWO workloads, which do not have the same optimum.
#
# (a) 24 trivial `claude -p` calls (no image, no reasoning) -- the spawn
# curve, model work held near zero:
#
#   workers   wall     throughput   mean latency
#      4      28.7s     0.84/s         4.5s
#      8      23.0s     1.04/s         6.9s
#     12      19.9s     1.21/s         9.5s   <- knee
#     16      23.6s     1.02/s        11.6s
#     24      32.1s     0.75/s        16.9s   <- slower than 4 workers
#
# Throughput peaks at 12 and DEGRADES above it, with zero rate-limit errors
# at every width -- precisely why the earlier "no errors, so no cap needed"
# evidence could not see this. A single trivial call costs 4.34s of pure
# process overhead, so the short-call ops (redact_text, classify_document,
# segment.judge -- all ~4.1-6.4s each, minima at ~4.1s) are mostly spawn
# cost rather than inference, and 12 is right for them.
#
# (b) 34 REAL scanned OCR pages -- the long-call workload:
#
#   workers   wall     per page   mean latency
#     12      75.2s     2.21s       23.5s
#     24      54.6s     1.61s       23.4s   <- knee
#     34      53.0s     1.56s       28.6s
#
# 27% faster at 24 than at 12 with per-call latency FLAT, so contention has
# not started; at 34 latency jumps +5.2s for a 1.6s wall gain, which is
# saturation. A real OCR call waits ~23s on the model with the local CPU
# idle, so spawn cost is a small share of it -- the same machine tolerates
# roughly twice the width when calls are long.
#
# This cap is set to 24, the HIGHER of the two, on purpose. It is a ceiling,
# not a target: each op still runs at its own measured width (OCR 24,
# redaction 12), and this exists to stop the pools from MULTIPLYING -- page
# workers x document workers is the demand (24 x 3 = 72 for OCR), which
# would otherwise put a multi-document case deep into saturation. Setting it
# to 12 would silently clamp OCR back to the width just measured 27% slower.
# HARNESS_LLM_MAX_INFLIGHT overrides it; 0 restores uncapped behaviour.
DEFAULT_LLM_MAX_INFLIGHT = 24  # measured OCR knee; see both curves above
LLM_MAX_INFLIGHT_ENV = "HARNESS_LLM_MAX_INFLIGHT"

_inflight_semaphore: threading.BoundedSemaphore | None = None
_inflight_limit: int | None = None
_inflight_lock = threading.Lock()


def _resolve_max_inflight(env: Mapping[str, str] | None = None) -> int:
    """Explicit env wins, then the default. 0 (or a negative/unparseable
    value) disables the cap entirely -- the documented rollback."""
    source = os.environ if env is None else env
    raw = str(source.get(LLM_MAX_INFLIGHT_ENV, "")).strip()
    if not raw:
        return DEFAULT_LLM_MAX_INFLIGHT
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_LLM_MAX_INFLIGHT
    return value if value > 0 else 0


def _get_inflight_semaphore():
    """The process-wide semaphore, built once. Returns None when disabled.

    Rebuilt if the resolved limit changes, so a test (or a caller that sets
    the env after import) is not stuck with the first value ever seen.
    """
    global _inflight_semaphore, _inflight_limit
    limit = _resolve_max_inflight()
    if limit <= 0:
        return None
    with _inflight_lock:
        if _inflight_semaphore is None or _inflight_limit != limit:
            _inflight_semaphore = threading.BoundedSemaphore(limit)
            _inflight_limit = limit
        return _inflight_semaphore


def reset_inflight_semaphore() -> None:
    """Drop the cached semaphore so the next call re-reads the env."""
    global _inflight_semaphore, _inflight_limit
    with _inflight_lock:
        _inflight_semaphore = None
        _inflight_limit = None


@contextlib.contextmanager
def provider_slot(op: str = "provider.call"):
    """Hold one in-flight permit for the duration of a single leaf call.

    The wait is traced as `provider.queue`, which is the only way queue time
    becomes visible at all -- without the semaphore there is no queue and the
    measurement does not exist.
    """
    sem = _get_inflight_semaphore()
    if sem is None:
        yield
        return
    acquired = sem.acquire(blocking=False)
    if not acquired:
        # Only pay for a span when the call actually waits; the uncontended
        # path stays free.
        with trace_mod.span("provider.queue", category="wait", op_name=op):
            sem.acquire()
    try:
        yield
    finally:
        sem.release()

# A child subprocess that reads untrusted claim-document images has no reason
# to hold other providers' credentials. Any env var whose name ends in one of
# these suffixes is stripped from a CLI child's environment unless it belongs
# to that CLI's own provider family (see _child_safe_env). A denylist (not an
# allowlist) so ordinary vars like PATH/HOME are never accidentally dropped.
_SECRET_ENV_SUFFIXES = ("_API_KEY", "_SECRET", "_TOKEN", "_ACCESS_KEY")


class ProviderConfigError(RuntimeError):
    """Raised when a provider is selected but not usable as configured."""


class ProviderExecutionError(RuntimeError):
    """Raised when a configured provider fails during execution."""


def _require_scan_images(image_paths) -> None:
    """The D2 content check is a VISION scan; running it with no images would
    silently scan nothing and could falsely report 'clear'. Fail closed."""
    if not image_paths:
        raise ProviderExecutionError(
            "scan_intake_content requires page images -- the D2 content check is a "
            "vision scan and cannot run blind (got no image_paths)"
        )


# Keywords claude-cli's --json-schema validator (ajv, strict mode) refuses.
# Verified against CLI 2.1.231: each one aborts argv construction with
# `--json-schema is not a valid JSON Schema`, before any provider call.
#   $schema          -- `no schema with key or ref ".../draft/2020-12/schema"`
#   dependentRequired -- `strict mode: unknown keyword`
# These are valid 2020-12 and MUST stay in the on-disk schema files, which are
# what validate_instance() enforces. This set is only about what that one CLI
# flag will parse.
_CLI_SCHEMA_UNSUPPORTED_KEYWORDS = frozenset({"$schema", "dependentRequired"})

# Claude CLI accepts its JSON Schema only as an inline argv value. Keep a
# deliberately conservative cap well below Windows CreateProcess's 32,767
# character command-line limit: the command path, flags, quoting, and future
# flags also consume that budget. Drivers with richer public contracts must
# send a compact transport schema and retain their full local validation gate.
CLAUDE_CLI_SCHEMA_MAX_CHARS = 8_000


def _cli_json_schema(output_schema: Mapping[str, Any]) -> dict[str, Any]:
    """The schema as claude-cli's --json-schema validator will accept it.

    Strips, recursively, the keywords in _CLI_SCHEMA_UNSUPPORTED_KEYWORDS. Two
    different losses, worth keeping distinct:

    `$schema` is a meta-schema declaration -- removing it changes nothing about
    what instances are valid.

    `dependentRequired` is a REAL constraint, and dropping it genuinely weakens
    the provider-side pre-check: the CLI will no longer reject a response that
    violates it. That is deliberate and bounded -- the authoritative gate is our
    own validate_instance() against the UNMODIFIED on-disk schema, which every
    candidate still passes through before any contract write. So a violating
    response is still caught, just by us rather than by the CLI (later, not
    never). Nothing reaches a governed file on the strength of this relaxation.

    Prefer expressing a constraint in an ajv-strict-compatible form over adding
    to the strip set: anything added here stops being enforced at the provider
    boundary for every caller.
    """
    def prune(node: Any) -> Any:
        if isinstance(node, Mapping):
            return {k: prune(v) for k, v in node.items()
                    if k not in _CLI_SCHEMA_UNSUPPORTED_KEYWORDS}
        if isinstance(node, list):
            return [prune(v) for v in node]
        return node

    return prune(dict(output_schema))


def _codex_native_schema_compatible(output_schema: Mapping[str, Any]) -> bool:
    """Whether Codex's strict JSON-schema mode can express this contract.

    Codex requires every object to close ``additionalProperties``.  Several
    harness contracts deliberately use a dynamic object (for example claim
    facts keyed by descriptive field name), which needs a *schema* in
    ``additionalProperties``.  That is not representable in Codex strict mode.
    The caller still validates the returned JSON against the complete local
    contract, so this predicate selects native enforcement only where it is
    faithful rather than narrowing a governed public schema to satisfy a CLI.
    """
    def visit(node: Any) -> bool:
        if isinstance(node, Mapping):
            if isinstance(node.get("additionalProperties"), Mapping):
                return False
            return all(visit(value) for value in node.values())
        if isinstance(node, list):
            return all(visit(value) for value in node)
        return True

    return visit(output_schema)


def _child_safe_env(*, keep_prefixes: Sequence[str]) -> dict[str, str]:
    """A copy of os.environ with foreign provider secrets removed.

    Keeps any secret-shaped var whose name starts with one of ``keep_prefixes``
    (the CLI's own provider family, which it needs to authenticate); drops every
    other secret-shaped var so a document-reading child can't exfiltrate, e.g.,
    OPENAI_API_KEY through a prompt-injected transcription.

    Deployments that back a CLI with a cloud provider whose credentials are not
    prefixed by the CLI's family name -- claude-via-Bedrock (AWS_SECRET_ACCESS_KEY)
    or claude-via-Vertex (GOOGLE_*) -- add those prefixes via the
    HARNESS_CHILD_ENV_KEEP_PREFIXES env var (comma-separated), so the child keeps
    the creds it needs without editing code. Matching is case-insensitive.

    KNOWN LIMITATION (accepted, not fixed): scrubbing keys on NAME SUFFIX
    (_API_KEY / _SECRET / _TOKEN / _ACCESS_KEY) covers the conventions every real
    LLM provider uses (OPENAI_API_KEY, GOOGLE_API_KEY, HF_TOKEN, COHERE/MISTRAL/
    GROQ_API_KEY, ...). A secret stored under an UNCONVENTIONAL name -- a bare
    `GEMINI_KEY` (ends _KEY, not _API_KEY), or a token in a randomly-named var --
    would NOT be recognized as a secret and could reach the child. Closing this
    fully needs an allowlist model (keep only known-safe vars, drop the rest),
    which risks dropping legitimate vars; the suffix denylist is the deliberate
    trade-off for the PoC.
    """
    extra = os.environ.get("HARNESS_CHILD_ENV_KEEP_PREFIXES", "")
    keep = tuple(p.strip().upper() for p in (*keep_prefixes, *extra.split(",")) if p.strip())
    safe = {}
    for key, value in os.environ.items():
        upper = key.upper()
        is_secret = any(upper.endswith(suffix) for suffix in _SECRET_ENV_SUFFIXES)
        if is_secret and not any(upper.startswith(prefix) for prefix in keep):
            continue
        safe[key] = value
    return safe


@dataclass(frozen=True)
class ProviderConfig:
    provider_name: str = DEFAULT_PROVIDER
    model_name: str | None = None


@dataclass(frozen=True)
class ProviderResult:
    provider_name: str
    model_name: str
    prompt_version: str
    text: str
    raw_metadata: dict[str, Any] = field(default_factory=dict)
    # Provider-reported completion signal where one exists (HTTP APIs). None for
    # CLI providers, which expose no finish reason. A value other than the
    # provider's normal-completion marker means the text may be truncated -- the
    # provider raises rather than returning a partial page (see _post_responses).
    finish_reason: str | None = None
    # Populated when the backend can enforce a caller-supplied output schema.
    # Callers must still perform their domain validation; this field records
    # that the transport returned a native structured-output value rather than
    # merely prose which happened to contain JSON.
    structured_output: dict[str, Any] | None = None

    def metadata(self) -> dict[str, Any]:
        return {
            "provider_name": self.provider_name,
            "model_name": self.model_name,
            "prompt_version": self.prompt_version,
            "raw_metadata": self.raw_metadata,
            "finish_reason": self.finish_reason,
        }


# The provider call surface, instrumented uniformly. Wrapping happens in
# BaseProvider.__init_subclass__ rather than by decorating each method,
# because every provider OVERRIDES these -- a decorator on the base class
# would be shadowed by the subclass and silently measure nothing. Hooking
# subclass creation catches every provider, present and future, including
# ones added later by someone who has never read this file.
_TRACED_PROVIDER_METHODS = (
    "transcribe_image",
    "analyze_image_structured",
    "compare_text",
    "classify_document",
    "scan_intake_content",
    "redact_text",
)


def _traced_provider_call(method_name, fn):
    """Wrap one provider method in a `provider.<method>` span.

    Records only sizes and identifiers -- never the prompt, never the returned
    page text. `input_chars`/`output_chars` stand in for tokens deliberately
    (tokens are out of scope for this instrumentation); they are already in
    hand, cost nothing to record, and carry no content.
    """
    op = f"provider.{method_name}"

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        if not trace_mod.enabled():
            return fn(self, *args, **kwargs)
        prompt = next((a for a in args if isinstance(a, str)), None)
        if prompt is None:
            prompt = kwargs.get("prompt")
        images = kwargs.get("image_paths")
        if images is None and method_name == "scan_intake_content":
            images = next((a for a in args if isinstance(a, (list, tuple))), None)
        image_count = len(images) if images else (
            1 if method_name in ("transcribe_image", "analyze_image_structured") else 0)
        with trace_mod.span(
            op, category="provider",
            provider_name=getattr(self, "provider_name", None),
            model_name=getattr(self, "model_name", None),
            input_chars=len(prompt) if isinstance(prompt, str) else None,
            input_images=image_count or None,
            structured=bool(kwargs.get("output_schema")) or None,
        ) as sp:
            result = fn(self, *args, **kwargs)
            text = getattr(result, "text", None)
            if isinstance(text, str):
                sp.set(output_chars=len(text))
            version = getattr(result, "prompt_version", None)
            if isinstance(version, str):
                sp.set(prompt_version=version)
            return result

    return wrapper


class BaseProvider:
    provider_name: str
    model_name: str

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        for name in _TRACED_PROVIDER_METHODS:
            fn = cls.__dict__.get(name)
            # Only wrap a method this class actually defines, and only once --
            # an intermediate subclass (_ApiProviderStub) would otherwise have
            # its already-wrapped method re-wrapped by its own children, double
            # counting the same call as two nested spans.
            if fn is None or getattr(fn, "_harness_traced", False):
                continue
            wrapped = _traced_provider_call(name, fn)
            wrapped._harness_traced = True
            setattr(cls, name, wrapped)

    def _result(
        self,
        text: str,
        prompt_version: str,
        raw_metadata: dict[str, Any] | None = None,
        *,
        finish_reason: str | None = None,
        structured_output: dict[str, Any] | None = None,
    ) -> ProviderResult:
        return ProviderResult(
            provider_name=self.provider_name,
            model_name=self.model_name,
            prompt_version=prompt_version,
            text=text,
            raw_metadata=raw_metadata or {},
            finish_reason=finish_reason,
            structured_output=structured_output,
        )

    def transcribe_image(self, image_path: Path, prompt: str, prompt_version: str) -> ProviderResult:
        raise NotImplementedError

    def analyze_image_structured(
        self,
        image_path: Path,
        prompt: str,
        prompt_version: str,
        output_schema: Mapping[str, Any],
    ) -> ProviderResult:
        """Analyze an image under a caller-owned structured-output contract.

        The default preserves provider portability: a backend without native
        schema enforcement still performs the image call, and the caller owns
        parsing, validation, and its one correction attempt. Providers with a
        native structured-output surface override this method.
        """
        return self.transcribe_image(image_path, prompt, prompt_version)

    def compare_text(
        self, prompt: str, prompt_version: str,
        output_schema: Mapping[str, Any] | None = None,
    ) -> ProviderResult:
        raise NotImplementedError

    def analyze_text_structured(
        self,
        prompt: str,
        prompt_version: str,
        output_schema: Mapping[str, Any],
    ) -> ProviderResult:
        """Return one native schema-constrained text-analysis result.

        This is distinct from ``compare_text``: drivers use it for candidate
        extraction, while comparison keeps its domain-specific prompts and
        timeout history. Implementations return the parsed mapping in
        ``structured_output`` and a stable JSON representation in ``text``.
        Domain correction remains the driver's one P4 correction attempt.
        """
        raise NotImplementedError

    def classify_document(self, prompt: str, prompt_version: str) -> ProviderResult:
        raise NotImplementedError

    def scan_intake_content(
        self, prompt: str, prompt_version: str, image_paths: Sequence[Path] | None = None
    ) -> ProviderResult:
        raise NotImplementedError

    def redact_text(self, prompt: str, prompt_version: str) -> ProviderResult:
        raise NotImplementedError


class ClaudeCliProvider(BaseProvider):
    provider_name = "claude-cli"

    def __init__(self, *, model_name: str | None = None, root: Path = ROOT, command: str = "claude"):
        self.model_name = model_name or "claude-cli"
        self.root = root
        self.command = command

    def _run(
        self,
        prompt: str,
        *,
        prompt_version: str,
        allowed_read: bool,
        timeout: int,
        read_cwd: Path | None = None,
        output_schema: Mapping[str, Any] | None = None,
    ) -> ProviderResult:
        # --safe-mode: the child claude -p session must see NOTHING but the
        # prompt -- no CLAUDE.md, skills, or session hooks. Without it, cwd=ROOT
        # auto-loads this project's context, and a context-aware reader
        # editorializes: CASE_022's real checkpoint-1 run had BOTH independent
        # reads append similar D2 meta-commentary to transcribed pages, which
        # compare() then waved through as material agreement (two-sided additions
        # defeat the one-sided-addition check from known-gaps item 11), landing
        # fabricated text in the trusted processed layer. This applies to every
        # claude-cli call through this provider (transcribe/compare/classify/scan),
        # not just OCR -- the same context-inheritance risk exists for all of them.
        # The prompt goes over STDIN, never argv. Windows CreateProcess caps a
        # command line at 32,767 chars, and V4a's consolidation call built a
        # 94,063-char argv (61,911 prompt + 32,152 schema) -- CreateProcess
        # failed with ERROR_FILE_NOT_FOUND, which Python raises as
        # FileNotFoundError, which this provider reported as "claude-cli
        # command not found" for a binary that plainly existed. Bare `-p` with
        # the prompt on stdin is the CLI's own documented form, so this costs
        # nothing and removes the prompt from the size budget entirely.
        cmd = [self.command, "-p", "--safe-mode"]
        if output_schema is not None:
            # --output-format json alone only wraps arbitrary assistant prose in
            # a JSON envelope. --json-schema is the part that requires a
            # validated value in the envelope's structured_output field.
            cli_schema = json.dumps(
                _cli_json_schema(output_schema), ensure_ascii=False, separators=(",", ":"),
            )
            if len(cli_schema) > CLAUDE_CLI_SCHEMA_MAX_CHARS:
                raise ProviderExecutionError(
                    "claude-cli structured-output schema is too large for inline argv "
                    f"({len(cli_schema)} chars; limit {CLAUDE_CLI_SCHEMA_MAX_CHARS}). "
                    "Use a compact transport schema and keep the full schema for local validation."
                )
            cmd.extend([
                "--output-format",
                "json",
                "--json-schema",
                cli_schema,
            ])
        # Pass the configured model through. Without this the model recorded in
        # provenance metadata is a lie (the CLI silently uses its own default),
        # and two readers configured with different models would be
        # indistinguishable. "claude-cli" is the no-model-configured sentinel.
        if self.model_name and self.model_name != "claude-cli":
            cmd.extend(["--model", self.model_name])
        if allowed_read:
            cmd.extend(["--allowedTools", "Read"])

        # D1/PII confinement (fleet review H1): a file-reading child runs with
        # cwd set to the IMAGE directory, not the repo root -- claude's
        # --allowedTools Read is scoped to cwd, so an injected "also read
        # data/ground_truth/...  / another case's PII" cannot reach case data
        # outside the scratch dir holding the rendered page image(s). Non-reading
        # calls (compare/classify/redact) keep cwd=root; they invoke no Read.
        cwd = str(read_cwd) if (allowed_read and read_cwd is not None) else str(self.root)

        # The child reads untrusted claim images; strip every non-Anthropic
        # secret from its environment so a prompt-injected read can't exfiltrate
        # another provider's key (see _child_safe_env).
        run_env = _child_safe_env(keep_prefixes=("ANTHROPIC", "CLAUDE"))

        # Bounded retry on transient subprocess-level failures. A single
        # failed claude-cli call otherwise kills an entire multi-page run:
        # checkpoint 1 only persists ocr_result after every page finishes, so
        # one hiccup on page N of a 75-page document (CASE_003/DOC_008 died
        # exactly this way at page 24 on "no stdin data received") discards all
        # prior pages. This retries ONLY the subprocess call itself
        # (FileNotFoundError is not transient and is not retried); it does NOT
        # touch P8 -- content agreement/disagreement is judged by compare(),
        # not here, so no disagreement tolerance is affected.
        last_exc: ProviderExecutionError | None = None
        # Diagnostic text from the most recent failure, fed to _retry_delay so a
        # server-supplied Retry-After can override the local backoff curve.
        last_detail = ""
        # Structured-output correction belongs to the caller (P4: exactly one
        # correction after validation failure). Do not hide extra whole-model
        # retries here. Ordinary subprocess calls retain their transient retry.
        max_attempts = 1 if output_schema is not None else _CLAUDE_CLI_MAX_ATTEMPTS
        for attempt in range(max_attempts):
            try:
                # input=prompt: the prompt is DELIVERED here, not in argv (see
                # the cmd construction above for the CreateProcess limit that
                # forces this). This also supersedes the former stdin=DEVNULL:
                # that existed to stop the child blocking ~3s on stdin it would
                # never get, and a child that receives its prompt on stdin --
                # then sees EOF as subprocess closes the pipe -- never waits at
                # all. The two are mutually exclusive; passing both raises.
                #
                # encoding/errors are set explicitly: the child CLI emits
                # UTF-8, but on a cp949-locale host bare text=True decodes with
                # the ANSI code page and crashes on Korean output
                # (UnicodeDecodeError -> stdout None). Same treatment
                # CodexCliProvider already applies.
                # The permit is held around this call ONLY -- not around the
                # retry backoff below, and never around a whole P8 chain.
                with provider_slot("claude-cli"):
                    result = subprocess.run(
                        cmd,
                        input=prompt,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=timeout,
                        cwd=cwd,
                        env=run_env,
                    )
            except FileNotFoundError as exc:
                raise ProviderExecutionError(f"claude-cli command not found: {self.command}") from exc
            except subprocess.TimeoutExpired as exc:
                last_exc = ProviderExecutionError(f"claude-cli call timed out after {timeout}s")
                last_exc.__cause__ = exc
                # A timeout carries no server diagnostic, so there is no
                # Retry-After to honour -- plain exponential backoff applies.
                last_detail = ""
            else:
                out = result.stdout.strip()
                # Fail closed on empty output even at exit 0. A blank string is
                # never a valid transcription/verdict/redaction, and the CLI has
                # been observed to exit non-zero with its diagnostic on STDOUT
                # (e.g. a bad --model prints the reason to stdout, not stderr) --
                # so an empty stdout can mask a real error. Better to retry/halt
                # than to write "" into the trusted layer as if it were content.
                if result.returncode == 0 and out:
                    raw_metadata = {
                        "command": cmd[0],
                        "returncode": result.returncode,
                        "stderr": result.stderr.strip(),
                        "attempts": attempt + 1,
                    }
                    if output_schema is None:
                        return self._result(out, prompt_version, raw_metadata)
                    try:
                        envelope = json.loads(out)
                    except json.JSONDecodeError as exc:
                        raise ProviderExecutionError(
                            "claude-cli structured-output mode returned a non-JSON envelope"
                        ) from exc
                    structured = envelope.get("structured_output") if isinstance(envelope, dict) else None
                    subtype = envelope.get("subtype") if isinstance(envelope, dict) else None
                    is_error = envelope.get("is_error") if isinstance(envelope, dict) else None
                    if not isinstance(structured, dict) or is_error is True or (
                        subtype is not None and subtype != "success"
                    ):
                        raise ProviderExecutionError(
                            "claude-cli did not return a successful structured_output "
                            f"(subtype={subtype!r}, is_error={is_error!r})"
                        )
                    raw_metadata.update({
                        "session_id": envelope.get("session_id"),
                        "subtype": subtype,
                        # Kept verbatim from the CLI envelope: token counts are
                        # the only way to tell a reasoning-heavy call from an
                        # output-heavy one. Arm G's M2 (822.5s) and M1 (134.2s)
                        # had near-identical visible input AND output, and with
                        # usage discarded the 6x gap was unexplainable from
                        # retained records.
                        "usage": envelope.get("usage"),
                        "model_usage": envelope.get("modelUsage"),
                    })
                    return self._result(
                        json.dumps(structured, ensure_ascii=False),
                        prompt_version,
                        raw_metadata,
                        structured_output=structured,
                    )
                # Surface whatever diagnostic exists: stderr first, then stdout
                # (where the CLI actually prints model/access errors), then a
                # last-resort exit-code note so the message is never empty.
                detail = result.stderr.strip() or out or f"exit {result.returncode}, no output"
                last_exc = ProviderExecutionError(f"claude-cli call failed: {detail}")
                last_detail = detail

            if attempt < max_attempts - 1:
                # attempt is 0-based; _retry_delay takes the 1-based count of
                # attempts already burned.
                time.sleep(_retry_delay(attempt + 1, last_detail))

        assert last_exc is not None
        raise last_exc

    # Transcription prompt is deliberately kept NEUTRAL -- no defensive
    # "this is a SANCTIONED step, do not refuse" framing. An earlier version
    # prepended exactly that role-framing block and it backfired: a prompt that
    # pre-argues its own legitimacy reads as a prompt-injection signal -- a
    # genuine OCR request never needs to defend itself -- so the child refused
    # and emitted meta-commentary instead of the page text. That lesson still
    # holds and is not being reverted here.
    #
    # The image is referenced as an explicit imperative ("Read the image file at
    # {path} and then ...") rather than a trailing "Image: {path}" label.
    # Investigation (2026-07-16) found the label form fails deterministically
    # (9/9, with and without --safe-mode): the model reads "Image: {path}" as
    # descriptive metadata about an attachment that -- via a text-only `claude -p`
    # call -- never actually arrives, so it never invokes the Read tool. Making
    # the read an explicit instruction resolves it. This carries no
    # self-legitimizing or anti-refusal language, so it gives the child nothing
    # to treat as a prompt-injection signal.
    def transcribe_image(self, image_path: Path, prompt: str, prompt_version: str) -> ProviderResult:
        framed_prompt = f"Read the image file at {image_path} and then: {prompt}"
        # Confine the child's Read to the image's own directory (H1).
        return self._run(framed_prompt, prompt_version=prompt_version, allowed_read=True,
                         timeout=180, read_cwd=Path(image_path).resolve().parent)

    def analyze_image_structured(
        self,
        image_path: Path,
        prompt: str,
        prompt_version: str,
        output_schema: Mapping[str, Any],
    ) -> ProviderResult:
        framed_prompt = f"Read the image file at {image_path} and then: {prompt}"
        return self._run(
            framed_prompt,
            prompt_version=prompt_version,
            allowed_read=True,
            timeout=180,
            read_cwd=Path(image_path).resolve().parent,
            output_schema=output_schema,
        )

    def compare_text(
        self, prompt: str, prompt_version: str,
        output_schema: Mapping[str, Any] | None = None,
    ) -> ProviderResult:
        return self._run(prompt, prompt_version=prompt_version, allowed_read=False,
                         timeout=compare_text_timeout(),
                         output_schema=output_schema)

    def analyze_text_structured(
        self,
        prompt: str,
        prompt_version: str,
        output_schema: Mapping[str, Any],
    ) -> ProviderResult:
        return self._run(
            prompt,
            prompt_version=prompt_version,
            allowed_read=False,
            timeout=structured_text_timeout(),
            output_schema=output_schema,
        )

    def classify_document(self, prompt: str, prompt_version: str) -> ProviderResult:
        return self._run(prompt, prompt_version=prompt_version, allowed_read=False, timeout=120)

    def scan_intake_content(
        self, prompt: str, prompt_version: str, image_paths: Sequence[Path] | None = None
    ) -> ProviderResult:
        _require_scan_images(image_paths)
        # The child opens the pages itself via its Read tool. Use the same
        # explicit-imperative form transcribe_image uses -- a trailing
        # "Image: {path}" label is read as metadata about a never-arriving
        # attachment and the Read tool is never invoked (2026-07-16 finding). An
        # HTTP provider attaches the images instead (see OpenAIApiProvider).
        refs = "\n".join(f"Read the image file at {p}." for p in image_paths)
        prompt = f"{prompt}\n\n{refs}"
        # Confine Read to the shared image directory when the pages live in one
        # dir (the intake scratch case), else leave unconfined (H1).
        parents = {Path(p).resolve().parent for p in image_paths}
        read_cwd = parents.pop() if len(parents) == 1 else None
        return self._run(prompt, prompt_version=prompt_version, allowed_read=True,
                         timeout=180, read_cwd=read_cwd)

    def redact_text(self, prompt: str, prompt_version: str) -> ProviderResult:
        return self._run(prompt, prompt_version=prompt_version, allowed_read=False, timeout=120)


class CodexCliProvider(BaseProvider):
    provider_name = "codex-cli"

    def __init__(
        self,
        *,
        model_name: str | None = None,
        root: Path = ROOT,
        command: str = "codex",
        env: Mapping[str, str] | None = None,
    ):
        self.model_name = model_name or "codex-cli"
        self.root = root
        self.command = command
        self.env = dict(env) if env is not None else dict(os.environ)

    def _run(
        self,
        prompt: str,
        *,
        prompt_version: str,
        timeout: int,
        image_paths: Sequence[Path] | None = None,
        output_schema: Mapping[str, Any] | None = None,
        structured_fallback: bool = False,
    ) -> ProviderResult:
        scratch_dir = self.root / "_ocr_scratch"
        scratch_dir.mkdir(parents=True, exist_ok=True)
        output_path: Path | None = None
        schema_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix="codex-last-message-",
                suffix=".txt",
                dir=scratch_dir,
                delete=False,
            ) as output_file:
                output_path = Path(output_file.name)

            if output_schema is not None:
                # Codex takes a schema FILE, unlike Claude's inline
                # --json-schema value. Closing this file before child launch
                # is required on Windows. The tool-owned scratch directory is
                # outside governed case data and neither path is persisted.
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    prefix="codex-output-schema-",
                    suffix=".json",
                    dir=scratch_dir,
                    delete=False,
                ) as schema_file:
                    json.dump(output_schema, schema_file, ensure_ascii=False,
                              separators=(",", ":"))
                    schema_path = Path(schema_file.name)

            # Feed the prompt on stdin rather than as a positional argument.
            # Claim-analysis's two-call driver serves whole redacted case
            # bundles, which can exceed Windows' CreateProcess command-line
            # limit.  On that platform the resulting WinError 2 is
            # indistinguishable from a missing executable at this layer.
            # ``codex exec -`` is the documented stdin form and keeps the
            # executable path/options short without changing model input.
            cmd = [self.command, "exec", "-", "--skip-git-repo-check", "--sandbox", "read-only"]
            if self.model_name != "codex-cli":
                cmd.extend(["--model", self.model_name])
            for image_path in image_paths or ():
                cmd.extend(["--image", str(image_path)])
            if schema_path is not None:
                cmd.extend(["--output-schema", str(schema_path)])
            cmd.extend(["--output-last-message", str(output_path)])

            # Start from a secret-scrubbed environment, then re-add only Codex's
            # own key -- a document-reading child shouldn't carry other
            # providers' credentials.
            run_env = _child_safe_env(keep_prefixes=("CODEX",))
            if self.env.get("CODEX_API_KEY"):
                run_env["CODEX_API_KEY"] = self.env["CODEX_API_KEY"]

            # Bounded retry on transient subprocess-level failures, mirroring
            # ClaudeCliProvider._run. This provider had NO retry path at all,
            # which mattered because it is checkpoint 2's dev-phase default:
            # the redaction loop was the least resilient path in the pipeline
            # (see the plan's B1) and page-level parallelism (T4c) makes a
            # transient failure more likely, not less, since N calls now hit
            # the backend at once.
            #
            # Scope is deliberately identical to the claude path: only the
            # subprocess CALL is retried. FileNotFoundError is not transient
            # (a missing binary stays missing) and is raised immediately.
            # Nothing here touches content judgment -- P8 agreement is decided
            # by compare(), and a redaction leak is decided by
            # redaction.py's own checks, so no tolerance is introduced.
            last_exc: ProviderExecutionError | None = None
            last_detail = ""
            # A native schema call is either transport-valid or the driver's
            # one correction case. Retrying a malformed semantic response here
            # would silently exceed P4's correction boundary.
            max_attempts = 1 if (output_schema is not None or structured_fallback) else _CLAUDE_CLI_MAX_ATTEMPTS
            for attempt in range(max_attempts):
                try:
                    with provider_slot("codex-cli"):
                        result = subprocess.run(
                            cmd,
                            capture_output=True,
                            text=True,
                            encoding="utf-8",
                            errors="replace",
                            input=prompt,
                            timeout=timeout,
                            cwd=str(self.root),
                            env=run_env,
                        )
                except FileNotFoundError as exc:
                    raise ProviderExecutionError(f"codex-cli command not found: {self.command}") from exc
                except subprocess.TimeoutExpired as exc:
                    # Previously unhandled here: a timeout escaped as a raw
                    # subprocess exception rather than a ProviderExecutionError,
                    # so callers that catch the provider error type saw an
                    # unexpected exception class instead of a normal failure.
                    last_exc = ProviderExecutionError(
                        f"codex-cli call timed out after {timeout}s")
                    last_exc.__cause__ = exc
                    last_detail = ""  # a timeout carries no server Retry-After
                else:
                    raw_metadata = {
                        "command": cmd[0],
                        "returncode": result.returncode,
                        "stderr": result.stderr.strip(),
                        "attempts": attempt + 1,
                    }
                    if result.returncode == 0:
                        text = output_path.read_text(encoding="utf-8").strip()
                        # Fail closed on empty output (same reason as the claude
                        # path): a blank result is never valid content, so never
                        # write it downstream. Retried rather than raised
                        # immediately, since an empty last-message file is the
                        # shape a truncated/aborted run takes.
                        if text:
                            if output_schema is None:
                                return self._result(text, prompt_version, raw_metadata)
                            try:
                                structured = json.loads(text)
                            except json.JSONDecodeError as exc:
                                raise ProviderExecutionError(
                                    "codex-cli structured-output mode returned non-JSON output"
                                ) from exc
                            if not isinstance(structured, dict):
                                raise ProviderExecutionError(
                                    "codex-cli structured-output mode returned a non-object value"
                                )
                            raw_metadata["structured_output_native"] = True
                            return self._result(
                                json.dumps(structured, ensure_ascii=False),
                                prompt_version,
                                raw_metadata,
                                structured_output=structured,
                            )
                        last_exc = ProviderExecutionError("codex-cli returned empty output")
                        last_detail = ""
                    else:
                        detail = (result.stderr.strip() or result.stdout.strip()
                                  or f"exit {result.returncode}, no output")
                        last_exc = ProviderExecutionError(f"codex-cli call failed: {detail}")
                        last_detail = detail

                if attempt < max_attempts - 1:
                    # Same full-jitter exponential curve as claude-cli, and for
                    # the same reason: page-level concurrency means several
                    # calls fail against a shared limit at nearly the same
                    # instant, so an un-jittered wait re-arrives as a herd.
                    time.sleep(_retry_delay(attempt + 1, last_detail))

            raise last_exc if last_exc is not None else ProviderExecutionError(
                "codex-cli call failed with no diagnostic")
        finally:
            if output_path is not None:
                output_path.unlink(missing_ok=True)
            if schema_path is not None:
                schema_path.unlink(missing_ok=True)

    def transcribe_image(self, image_path: Path, prompt: str, prompt_version: str) -> ProviderResult:
        return self._run(
            prompt,
            prompt_version=prompt_version,
            timeout=180,
            image_paths=[image_path],
        )

    def compare_text(
        self, prompt: str, prompt_version: str,
        output_schema: Mapping[str, Any] | None = None,
    ) -> ProviderResult:
        return self._run(prompt, prompt_version=prompt_version,
                         timeout=compare_text_timeout(), output_schema=output_schema)

    def analyze_text_structured(
        self,
        prompt: str,
        prompt_version: str,
        output_schema: Mapping[str, Any],
    ) -> ProviderResult:
        if _codex_native_schema_compatible(output_schema):
            return self._run(
                prompt,
                prompt_version=prompt_version,
                timeout=compare_text_timeout(),
                output_schema=output_schema,
            )

        # Do not distort a dynamic harness contract merely to fit Codex's
        # closed-object response-format dialect.  The driver supplies an
        # explicit JSON-only prompt and performs its own schema validation
        # before any DAO candidate/contract write; its existing P4 correction
        # boundary remains the semantic recovery path.
        raw = self._run(
            prompt,
            prompt_version=prompt_version,
            timeout=structured_text_timeout(),
            structured_fallback=True,
        )
        try:
            structured = json.loads(raw.text)
        except json.JSONDecodeError as exc:
            raise ProviderExecutionError(
                "codex-cli fallback structured-output mode returned non-JSON output"
            ) from exc
        if not isinstance(structured, dict):
            raise ProviderExecutionError(
                "codex-cli fallback structured-output mode returned a non-object value"
            )
        metadata = dict(raw.raw_metadata)
        metadata["structured_output_native"] = False
        return self._result(
            json.dumps(structured, ensure_ascii=False),
            prompt_version,
            metadata,
            structured_output=structured,
        )

    def classify_document(self, prompt: str, prompt_version: str) -> ProviderResult:
        return self._run(prompt, prompt_version=prompt_version, timeout=120)

    def scan_intake_content(
        self, prompt: str, prompt_version: str, image_paths: Sequence[Path] | None = None
    ) -> ProviderResult:
        _require_scan_images(image_paths)
        return self._run(
            prompt, prompt_version=prompt_version, timeout=180, image_paths=image_paths
        )

    def redact_text(self, prompt: str, prompt_version: str) -> ProviderResult:
        return self._run(prompt, prompt_version=prompt_version, timeout=120)


class _ApiProviderStub(BaseProvider):
    required_key_env: str
    model_env_names: tuple[str, ...] = ()

    def __init__(self, *, model_name: str | None, env: Mapping[str, str]):
        api_key = env.get(self.required_key_env)
        if not api_key:
            raise ProviderConfigError(f"{self.provider_name} requires {self.required_key_env}")
        self.api_key = api_key
        self.model_name = model_name or self._default_model_name(env)

    def _default_model_name(self, env: Mapping[str, str]) -> str:
        for env_name in self.model_env_names:
            if env.get(env_name):
                return env[env_name]
        # No silent fabrication: a made-up "<provider>-model" string only fails
        # later with an opaque 404 at the first page. Fail at selection instead.
        env_hint = " or ".join(self.model_env_names) if self.model_env_names else "the provider env"
        raise ProviderConfigError(
            f"{self.provider_name} requires a model name via --model or {env_hint}"
        )

    def _not_implemented(self) -> ProviderExecutionError:
        return ProviderExecutionError(
            f"{self.provider_name} is selectable, but API execution is not implemented yet"
        )

    def transcribe_image(self, image_path: Path, prompt: str, prompt_version: str) -> ProviderResult:
        raise self._not_implemented()

    def compare_text(
        self, prompt: str, prompt_version: str,
        output_schema: Mapping[str, Any] | None = None,
    ) -> ProviderResult:
        raise self._not_implemented()

    def classify_document(self, prompt: str, prompt_version: str) -> ProviderResult:
        raise self._not_implemented()

    def scan_intake_content(
        self, prompt: str, prompt_version: str, image_paths: Sequence[Path] | None = None
    ) -> ProviderResult:
        raise self._not_implemented()

    def redact_text(self, prompt: str, prompt_version: str) -> ProviderResult:
        raise self._not_implemented()


class AnthropicApiProvider(_ApiProviderStub):
    """Direct Messages-API backend. Only ``analyze_text_structured`` is live.

    Exists for the P2-(a) driver reassessment: the cold ``claude -p``
    structured call costs 200-330s nearly regardless of payload, and the only
    way to know whether that is CLI overhead or model latency is a direct API
    arm on the same prompt. Image/OCR paths stay unimplemented -- P8's
    provider-exposure notes were written for the CLI paths, and nothing sends
    raw page images here.

    Prompt caching: the user text block carries ``cache_control`` ephemeral.
    Anthropic's cache key is a prefix over tools -> system -> messages, and
    each checkpoint sends a DIFFERENT transport schema as its forced tool, so
    cross-checkpoint reuse of the shared case-material prefix does NOT happen
    under this shape -- only an identical re-call (a P4 correction round
    resending the same material, a rerun inside the 5-minute TTL) gets read
    hits. ``usage.cache_read_input_tokens`` in raw_metadata is the ground
    truth for whether a given call actually hit.
    """

    provider_name = "anthropic-api"
    required_key_env = "ANTHROPIC_API_KEY"
    model_env_names = ("HARNESS_ANTHROPIC_MODEL", "ANTHROPIC_MODEL")
    STRUCTURED_TOOL_NAME = "emit_result"

    def __init__(
        self,
        *,
        model_name: str | None,
        env: Mapping[str, str],
        base_url: str | None = None,
    ):
        super().__init__(model_name=model_name, env=env)
        self.base_url = (
            base_url or env.get("ANTHROPIC_BASE_URL") or DEFAULT_ANTHROPIC_BASE_URL
        ).rstrip("/")
        raw_cap = env.get(_ANTHROPIC_MAX_TOKENS_ENV, "")
        try:
            cap = int(raw_cap) if raw_cap else _ANTHROPIC_MAX_TOKENS_DEFAULT
        except ValueError:
            raise ProviderConfigError(
                f"{_ANTHROPIC_MAX_TOKENS_ENV} must be an integer, got {raw_cap!r}"
            )
        if cap <= 0:
            raise ProviderConfigError(f"{_ANTHROPIC_MAX_TOKENS_ENV} must be positive")
        self.max_output_tokens = cap

    def analyze_text_structured(
        self,
        prompt: str,
        prompt_version: str,
        output_schema: Mapping[str, Any],
    ) -> ProviderResult:
        # Structured output via forced tool use: the schema rides as the one
        # tool's input_schema and tool_choice pins it, so the model must
        # answer through it. The API conforms tool inputs to the schema but is
        # not a hard validator -- the caller's validate_instance() gate against
        # the unmodified on-disk schema stays authoritative, same as the CLI
        # path (see _cli_json_schema; its keyword strip is harmless here and
        # keeps the two transports byte-comparable).
        payload = {
            "model": self.model_name,
            "max_tokens": self.max_output_tokens,
            "tools": [{
                "name": self.STRUCTURED_TOOL_NAME,
                "description": (
                    "Return the complete analysis result in the required structure."
                ),
                "input_schema": _cli_json_schema(output_schema),
            }],
            "tool_choice": {"type": "tool", "name": self.STRUCTURED_TOOL_NAME},
            "messages": [{
                "role": "user",
                "content": [{
                    "type": "text",
                    "text": prompt,
                    "cache_control": {"type": "ephemeral"},
                }],
            }],
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/v1/messages",
            data=body,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        timeout = structured_text_timeout()
        try:
            with provider_slot("anthropic-api"), \
                    urllib.request.urlopen(request, timeout=timeout) as response:
                response_body = response.read().decode("utf-8")
                status_code = getattr(response, "status", None)
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise ProviderExecutionError(
                f"anthropic-api call failed ({exc.code}): {error_body}"
            ) from exc
        except urllib.error.URLError as exc:
            raise ProviderExecutionError(f"anthropic-api call failed: {exc.reason}") from exc

        try:
            parsed = json.loads(response_body)
        except json.JSONDecodeError as exc:
            raise ProviderExecutionError(
                f"anthropic-api returned non-JSON response: {response_body!r}"
            ) from exc

        # Truncation guard, same posture as the other providers: a response cut
        # off at max_tokens carries a syntactically broken or silently partial
        # tool input. Refuse it rather than validate half a result.
        stop_reason = parsed.get("stop_reason")
        if stop_reason == "max_tokens":
            raise ProviderExecutionError(
                "anthropic-api response was truncated at max_tokens "
                f"({self.max_output_tokens}); refusing to use a partial result. "
                f"Raise {_ANTHROPIC_MAX_TOKENS_ENV} if the output is legitimately larger."
            )

        content = parsed.get("content")
        blocks = content if isinstance(content, list) else []
        tool_block = next(
            (b for b in blocks
             if isinstance(b, Mapping) and b.get("type") == "tool_use"
             and b.get("name") == self.STRUCTURED_TOOL_NAME),
            None,
        )
        structured = tool_block.get("input") if tool_block is not None else None
        if not isinstance(structured, dict):
            raise ProviderExecutionError(
                "anthropic-api did not return a structured tool_use result "
                f"(stop_reason={stop_reason!r})"
            )
        return self._result(
            json.dumps(structured, ensure_ascii=False),
            prompt_version,
            {
                "response_id": parsed.get("id"),
                "stop_reason": stop_reason,
                "http_status": status_code,
                "usage": parsed.get("usage"),
            },
            finish_reason=stop_reason,
            structured_output=structured,
        )


class OpenAIApiProvider(_ApiProviderStub):
    provider_name = "openai-api"
    required_key_env = "OPENAI_API_KEY"
    model_env_names = ("HARNESS_OPENAI_MODEL", "OPENAI_MODEL")
    default_model_name = "gpt-4.1"

    def __init__(
        self,
        *,
        model_name: str | None,
        env: Mapping[str, str],
        base_url: str | None = None,
    ):
        super().__init__(model_name=model_name, env=env)
        self.model_name = model_name or self._default_model_name(env)
        self.base_url = (base_url or env.get("OPENAI_BASE_URL") or DEFAULT_OPENAI_BASE_URL).rstrip("/")

    def _default_model_name(self, env: Mapping[str, str]) -> str:
        for env_name in self.model_env_names:
            if env.get(env_name):
                return env[env_name]
        return self.default_model_name

    def _post_responses(self, input_payload, *, prompt_version: str, timeout: int) -> ProviderResult:
        payload = {"model": self.model_name, "input": input_payload}
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/responses",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with provider_slot("openai-api"), \
                    urllib.request.urlopen(request, timeout=timeout) as response:
                response_body = response.read().decode("utf-8")
                status_code = getattr(response, "status", None)
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise ProviderExecutionError(f"openai-api call failed ({exc.code}): {error_body}") from exc
        except urllib.error.URLError as exc:
            raise ProviderExecutionError(f"openai-api call failed: {exc.reason}") from exc

        try:
            parsed = json.loads(response_body)
        except json.JSONDecodeError as exc:
            raise ProviderExecutionError(f"openai-api returned non-JSON response: {response_body!r}") from exc

        # Truncation guard (verified against the Responses API contract): a
        # response cut off at the token cap comes back status "incomplete" with
        # incomplete_details.reason "max_output_tokens" and PARTIAL (or, for a
        # reasoning model, empty) output_text. Returning that as "the
        # transcription"/"the redaction" would silently land half a page in the
        # trusted layer. Refuse it. incomplete_details can be null even when
        # incomplete -- hence the `or {}`.
        status = parsed.get("status")
        if status == "incomplete":
            reason = (parsed.get("incomplete_details") or {}).get("reason", "unknown")
            raise ProviderExecutionError(
                f"openai-api response was truncated (status=incomplete, reason={reason}); "
                "refusing to use a partial result"
            )
        # Any non-completed terminal status (e.g. "failed") is a failure too --
        # only "completed" is trusted. None is left permissive (older responses
        # may omit status; the output_text check below still fails closed).
        if status is not None and status != "completed":
            raise ProviderExecutionError(
                f"openai-api response was not completed (status={status!r}); refusing to use it"
            )

        text = _extract_openai_output_text(parsed)
        if text is None or not text.strip():
            # Fail closed on empty output, matching the CLI providers: a blank
            # string is never valid content and can mask a reasoning-only or
            # otherwise-degenerate completion.
            raise ProviderExecutionError("openai-api response contained no usable output_text")
        return self._result(
            text.strip(),
            prompt_version,
            {
                "response_id": parsed.get("id"),
                "status": status,
                "http_status": status_code,
                "usage": parsed.get("usage"),
            },
            finish_reason=status,
        )

    def transcribe_image(self, image_path: Path, prompt: str, prompt_version: str) -> ProviderResult:
        input_payload = [{
            "role": "user",
            "content": [
                {"type": "input_text", "text": prompt},
                *_image_content_parts([image_path]),
            ],
        }]
        return self._post_responses(input_payload, prompt_version=prompt_version, timeout=180)

    def compare_text(
        self, prompt: str, prompt_version: str,
        output_schema: Mapping[str, Any] | None = None,
    ) -> ProviderResult:
        # output_schema accepted for interface parity; _post_responses has no
        # response_format/json_schema wiring yet, so it is not enforced here.
        return self._post_responses(prompt, prompt_version=prompt_version, timeout=60)

    def classify_document(self, prompt: str, prompt_version: str) -> ProviderResult:
        return self._post_responses(prompt, prompt_version=prompt_version, timeout=120)

    def scan_intake_content(
        self, prompt: str, prompt_version: str, image_paths: Sequence[Path] | None = None
    ) -> ProviderResult:
        _require_scan_images(image_paths)
        # The whole point of the D2 scan is that the model SEES the page images.
        # Attach them as image content parts; a bare text prompt naming file
        # paths reaches the API as dead strings the server cannot open.
        input_payload = [{
            "role": "user",
            "content": [
                {"type": "input_text", "text": prompt},
                *_image_content_parts(image_paths or ()),
            ],
        }]
        return self._post_responses(input_payload, prompt_version=prompt_version, timeout=180)

    def redact_text(self, prompt: str, prompt_version: str) -> ProviderResult:
        return self._post_responses(prompt, prompt_version=prompt_version, timeout=120)


class OpenRouterProvider(_ApiProviderStub):
    """OpenAI-compatible chat-completions backend, routed through OpenRouter.

    The default provider. It implements the WHOLE provider surface -- both
    vision paths (OCR transcription, the D2 intake scan), both structured paths
    (text and image), and the plain text paths -- so selecting it replaces the
    CLI transport outright rather than covering a subset of it, the way
    ``anthropic-api`` (text-structured only) does.

    Two deliberate differences from the CLI providers, both consequences of
    HTTP replacing a subprocess:

    * **Images are attached, not read.** ``ClaudeCliProvider`` hands the child a
      path and lets its Read tool open the file, which is why it needs
      ``--safe-mode``, an ``--allowedTools Read`` allowlist, a cwd confined to
      the image directory, and a scrubbed child environment. None of that
      applies here: there is no child, no filesystem access, and no ambient
      project context to inherit -- the request carries exactly the prompt and
      the base64 image bytes and nothing else. The D1/PII confinement those
      flags provide is structural on this path.
    * **Structured output rides a forced tool call**, as in
      ``AnthropicApiProvider``, not ``response_format: json_schema``. The strict
      json_schema mode requires every object to close ``additionalProperties``,
      which several harness contracts deliberately do not (a claim-facts map
      keyed by field name needs a *schema* there -- see
      ``_codex_native_schema_compatible``). A forced single-function tool
      expresses those contracts unchanged. Provider-side conformance is still
      only a pre-check: ``validate_instance()`` against the unmodified on-disk
      schema stays the authoritative gate, and the caller keeps its one P4
      correction attempt.
    """

    provider_name = "openrouter"
    required_key_env = "OPENROUTER_API_KEY"
    model_env_names = ("HARNESS_OPENROUTER_MODEL", "OPENROUTER_MODEL")
    STRUCTURED_TOOL_NAME = "emit_result"

    def __init__(
        self,
        *,
        model_name: str | None,
        env: Mapping[str, str],
        base_url: str | None = None,
    ):
        super().__init__(model_name=model_name, env=env)
        self.base_url = (
            base_url or env.get("OPENROUTER_BASE_URL") or DEFAULT_OPENROUTER_BASE_URL
        ).rstrip("/")
        _require_transport_security(self.base_url, env)
        collection = (env.get(_OPENROUTER_DATA_COLLECTION_ENV)
                      or _OPENROUTER_DATA_COLLECTION_DEFAULT).strip().lower()
        if collection not in _OPENROUTER_DATA_COLLECTION_VALUES:
            raise ProviderConfigError(
                f"{_OPENROUTER_DATA_COLLECTION_ENV} must be one of "
                f"{', '.join(_OPENROUTER_DATA_COLLECTION_VALUES)}, got {collection!r}"
            )
        self.data_collection = collection
        self.zero_data_retention = str(env.get(_OPENROUTER_ZDR_ENV, "")).strip() == "1"
        raw_cap = env.get(_OPENROUTER_MAX_TOKENS_ENV, "")
        try:
            cap = int(raw_cap) if raw_cap else _OPENROUTER_MAX_TOKENS_DEFAULT
        except ValueError:
            raise ProviderConfigError(
                f"{_OPENROUTER_MAX_TOKENS_ENV} must be an integer, got {raw_cap!r}"
            )
        if cap <= 0:
            raise ProviderConfigError(f"{_OPENROUTER_MAX_TOKENS_ENV} must be positive")
        self.max_output_tokens = cap
        self.referer = env.get(_OPENROUTER_REFERER_ENV) or None
        self.title = env.get(_OPENROUTER_TITLE_ENV) or None
        self.user_agent = (
            env.get(_OPENROUTER_USER_AGENT_ENV) or _OPENROUTER_DEFAULT_USER_AGENT)

    # No model_env_names fallback to a hard-coded slug: OpenRouter model ids are
    # vendor-prefixed and change constantly, and a fabricated default only fails
    # later as an opaque 404 on the first page. _ApiProviderStub already fails at
    # selection instead, which is the behaviour wanted here.

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
        }
        if self.referer:
            headers["HTTP-Referer"] = self.referer
        if self.title:
            headers["X-Title"] = self.title
        return headers

    def _post_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        prompt_version: str,
        timeout: int,
        output_schema: Mapping[str, Any] | None = None,
    ) -> ProviderResult:
        routing: dict[str, Any] = {"data_collection": self.data_collection}
        if self.zero_data_retention:
            routing["zdr"] = True
        payload: dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
            "max_tokens": self.max_output_tokens,
            # Sent on EVERY call, never only on the ones handling images: a
            # redaction prompt carries the same material the page did.
            "provider": routing,
        }
        if output_schema is not None:
            payload["tools"] = [{
                "type": "function",
                "function": {
                    "name": self.STRUCTURED_TOOL_NAME,
                    "description": (
                        "Return the complete analysis result in the required structure."
                    ),
                    "parameters": _cli_json_schema(output_schema),
                },
            }]
            payload["tool_choice"] = {
                "type": "function",
                "function": {"name": self.STRUCTURED_TOOL_NAME},
            }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        last_exc: ProviderExecutionError | None = None
        for attempt in range(_OPENROUTER_MAX_ATTEMPTS):
            request = urllib.request.Request(
                f"{self.base_url}/chat/completions",
                data=body,
                headers=self._headers(),
                method="POST",
            )
            retry_detail: str | None = None
            try:
                # The permit covers the single round trip only, never a whole
                # P8 chain, and is released across the backoff sleep below --
                # same rule as every other provider (see provider_slot).
                with provider_slot("openrouter"), \
                        urllib.request.urlopen(request, timeout=timeout) as response:
                    response_body = response.read().decode("utf-8")
                    status_code = getattr(response, "status", None)
            except urllib.error.HTTPError as exc:
                error_body = _openrouter_error_body(
                    exc.read().decode("utf-8", errors="replace"))
                message = f"openrouter call failed ({exc.code}): {error_body}"
                if exc.code in _OPENROUTER_RETRY_STATUSES:
                    last_exc = ProviderExecutionError(message)
                    # Feed _retry_delay the server's own Retry-After when it
                    # sent one -- a header is authoritative over the local
                    # curve. It parses the delta-seconds form out of free text,
                    # so the header is folded into the detail string.
                    header = exc.headers.get("Retry-After") if exc.headers else None
                    retry_detail = (
                        f"retry-after: {header}\n{error_body}" if header else error_body
                    )
                else:
                    raise ProviderExecutionError(message) from exc
            except urllib.error.URLError as exc:
                # Connection-level failure: no request was ever completed, so
                # retrying cannot discard a model result.
                last_exc = ProviderExecutionError(f"openrouter call failed: {exc.reason}")
                last_exc.__cause__ = exc
                retry_detail = str(exc.reason)
            except TimeoutError as exc:
                last_exc = ProviderExecutionError(f"openrouter call timed out after {timeout}s")
                last_exc.__cause__ = exc
                retry_detail = ""
            except (http.client.HTTPException, ConnectionError) as exc:
                # The response DIED MID-BODY: a truncated read against a
                # declared Content-Length (IncompleteRead), a reset connection,
                # a malformed status line. urlopen has already returned by
                # then, so neither HTTPError nor URLError covers this and the
                # raw exception would escape past the retry loop into the
                # caller as something other than a ProviderExecutionError.
                # Retryable on the same reasoning as the rest: a partial body
                # is not a completion, so re-sending discards no model output.
                last_exc = ProviderExecutionError(
                    f"openrouter response body did not arrive intact: "
                    f"{type(exc).__name__}: {exc}")
                last_exc.__cause__ = exc
                retry_detail = str(exc)
            else:
                return self._parse_chat_response(
                    response_body,
                    prompt_version=prompt_version,
                    status_code=status_code,
                    structured=output_schema is not None,
                )

            if attempt < _OPENROUTER_MAX_ATTEMPTS - 1:
                time.sleep(_retry_delay(attempt + 1, retry_detail or ""))

        assert last_exc is not None
        raise last_exc

    def _parse_chat_response(
        self,
        response_body: str,
        *,
        prompt_version: str,
        status_code: int | None,
        structured: bool,
    ) -> ProviderResult:
        try:
            parsed = json.loads(response_body)
        except json.JSONDecodeError as exc:
            raise ProviderExecutionError(
                f"openrouter returned non-JSON response: {response_body!r}"
            ) from exc

        # OpenRouter can answer 200 OK with a top-level `error` object (an
        # upstream provider that failed after the response was committed, a
        # moderation block). Nothing below would notice -- `choices` is absent
        # or empty -- but the error text is the useful diagnostic, so read it
        # first rather than reporting "no choices".
        error = parsed.get("error")
        if isinstance(error, Mapping):
            raise ProviderExecutionError(
                f"openrouter returned an error (code={error.get('code')!r}): "
                f"{error.get('message')!r}"
            )

        choices = parsed.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices else None
        if not isinstance(choice, Mapping):
            raise ProviderExecutionError(
                f"openrouter response contained no choices (http {status_code})"
            )
        finish_reason = choice.get("finish_reason")
        # native_finish_reason is the upstream provider's own word for it, kept
        # because OpenRouter normalizes several distinct upstream conditions
        # onto one value and the raw one is what makes a run diagnosable.
        native_finish_reason = choice.get("native_finish_reason")
        if finish_reason in _OPENROUTER_BAD_FINISH_REASONS:
            raise ProviderExecutionError(
                f"openrouter response was not usable (finish_reason={finish_reason!r}, "
                f"native_finish_reason={native_finish_reason!r}); refusing to use a "
                "partial or blocked result. Raise "
                f"{_OPENROUTER_MAX_TOKENS_ENV} if the output is legitimately larger "
                f"than {self.max_output_tokens} tokens."
            )

        message = choice.get("message")
        message = message if isinstance(message, Mapping) else {}
        raw_metadata = {
            "response_id": parsed.get("id"),
            "http_status": status_code,
            "finish_reason": finish_reason,
            "native_finish_reason": native_finish_reason,
            # OpenRouter reports the model that ACTUALLY served the request,
            # which can differ from the requested slug (a routed fallback). The
            # provenance record must carry what ran, not what was asked for.
            "served_model": parsed.get("model"),
            "served_provider": parsed.get("provider"),
            # The PII posture the call was made under, recorded with the call
            # rather than inferred later from whatever the env holds today.
            "data_collection": self.data_collection,
            "zdr": self.zero_data_retention,
            # Usage arrives on every response; token counts are the only way to
            # tell a reasoning-heavy call from an output-heavy one after the fact.
            "usage": parsed.get("usage"),
        }

        if structured:
            arguments = _openrouter_tool_arguments(message, self.STRUCTURED_TOOL_NAME)
            if arguments is None:
                raise ProviderExecutionError(
                    "openrouter did not return the forced "
                    f"{self.STRUCTURED_TOOL_NAME} tool call "
                    f"(finish_reason={finish_reason!r})"
                )
            return self._result(
                json.dumps(arguments, ensure_ascii=False),
                prompt_version,
                raw_metadata,
                finish_reason=finish_reason,
                structured_output=arguments,
            )

        text = _openrouter_message_text(message)
        if text is None or not text.strip():
            # Fail closed on empty output, matching every other provider: a
            # blank string is never a valid transcription/verdict/redaction and
            # can mask a reasoning-only or otherwise degenerate completion.
            raise ProviderExecutionError(
                "openrouter response contained no usable message content "
                f"(finish_reason={finish_reason!r})"
            )
        return self._result(
            text.strip(), prompt_version, raw_metadata, finish_reason=finish_reason,
        )

    def _user_message(
        self, prompt: str, image_paths: Sequence[Path] = ()
    ) -> list[dict[str, Any]]:
        if not image_paths:
            return [{"role": "user", "content": prompt}]
        return [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                *_chat_image_content_parts(image_paths),
            ],
        }]

    def transcribe_image(self, image_path: Path, prompt: str, prompt_version: str) -> ProviderResult:
        return self._post_chat(
            self._user_message(prompt, [image_path]),
            prompt_version=prompt_version,
            timeout=180,
        )

    def analyze_image_structured(
        self,
        image_path: Path,
        prompt: str,
        prompt_version: str,
        output_schema: Mapping[str, Any],
    ) -> ProviderResult:
        return self._post_chat(
            self._user_message(prompt, [image_path]),
            prompt_version=prompt_version,
            timeout=180,
            output_schema=output_schema,
        )

    def compare_text(
        self, prompt: str, prompt_version: str,
        output_schema: Mapping[str, Any] | None = None,
    ) -> ProviderResult:
        return self._post_chat(
            self._user_message(prompt),
            prompt_version=prompt_version,
            timeout=compare_text_timeout(),
            output_schema=output_schema,
        )

    def analyze_text_structured(
        self,
        prompt: str,
        prompt_version: str,
        output_schema: Mapping[str, Any],
    ) -> ProviderResult:
        return self._post_chat(
            self._user_message(prompt),
            prompt_version=prompt_version,
            timeout=structured_text_timeout(),
            output_schema=output_schema,
        )

    def classify_document(self, prompt: str, prompt_version: str) -> ProviderResult:
        return self._post_chat(
            self._user_message(prompt), prompt_version=prompt_version, timeout=120,
        )

    def scan_intake_content(
        self, prompt: str, prompt_version: str, image_paths: Sequence[Path] | None = None
    ) -> ProviderResult:
        _require_scan_images(image_paths)
        # The D2 scan only means anything if the model SEES the pages; attach
        # them as image parts. A bare text prompt naming file paths reaches the
        # API as dead strings no server can open.
        return self._post_chat(
            self._user_message(prompt, image_paths or ()),
            prompt_version=prompt_version,
            timeout=180,
        )

    def redact_text(self, prompt: str, prompt_version: str) -> ProviderResult:
        return self._post_chat(
            self._user_message(prompt), prompt_version=prompt_version, timeout=120,
        )


class FixtureProvider(BaseProvider):
    provider_name = "fixture"

    def __init__(
        self,
        *,
        model_name: str | None = None,
        text: str = "",
        responses: Mapping[str, str] | None = None,
    ):
        self.model_name = model_name or "fixture"
        self.text = text
        self.responses = dict(responses or {})

    def _response(self, key: str, prompt_version: str) -> ProviderResult:
        return self._result(self.responses.get(key, self.text), prompt_version, {"fixture_key": key})

    def transcribe_image(self, image_path: Path, prompt: str, prompt_version: str) -> ProviderResult:
        return self._response("transcribe_image", prompt_version)

    def compare_text(
        self, prompt: str, prompt_version: str,
        output_schema: Mapping[str, Any] | None = None,
    ) -> ProviderResult:
        return self._response("compare_text", prompt_version)

    def analyze_text_structured(
        self,
        prompt: str,
        prompt_version: str,
        output_schema: Mapping[str, Any],
    ) -> ProviderResult:
        """Return fixture JSON through the same structured-result seam.

        Fixture is used by driver tests and must exercise the native-output
        consumer path rather than silently falling back to BaseProvider's
        NotImplementedError.  The fixture deliberately does not validate the
        schema: production providers enforce that boundary, while tests can
        supply malformed JSON to cover driver refusal behaviour.
        """
        result = self._response("analyze_text_structured", prompt_version)
        try:
            parsed = json.loads(result.text)
        except json.JSONDecodeError as exc:
            raise ProviderExecutionError("fixture structured response is not JSON") from exc
        if not isinstance(parsed, dict):
            raise ProviderExecutionError("fixture structured response must be a JSON object")
        return self._result(
            json.dumps(parsed, ensure_ascii=False), prompt_version,
            result.raw_metadata, structured_output=parsed,
        )

    def classify_document(self, prompt: str, prompt_version: str) -> ProviderResult:
        return self._response("classify_document", prompt_version)

    def scan_intake_content(
        self, prompt: str, prompt_version: str, image_paths: Sequence[Path] | None = None
    ) -> ProviderResult:
        return self._response("scan_intake_content", prompt_version)

    def redact_text(self, prompt: str, prompt_version: str) -> ProviderResult:
        return self._response("redact_text", prompt_version)


def add_provider_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--provider", choices=SUPPORTED_PROVIDERS, help="LLM provider backend")
    parser.add_argument("--model", help="Provider model name or deployment identifier")


def parse_provider_config(
    args: argparse.Namespace | None = None,
    *,
    env: Mapping[str, str] | None = None,
    env_prefix: str = DEFAULT_ENV_PREFIX,
    default_provider: str = DEFAULT_PROVIDER,
) -> ProviderConfig:
    source_env = env if env is not None else os.environ
    arg_provider = getattr(args, "provider", None) if args is not None else None
    arg_model = getattr(args, "model", None) if args is not None else None
    env_provider = source_env.get(f"{env_prefix}_PROVIDER")
    env_model = source_env.get(f"{env_prefix}_MODEL")

    provider_name = _normalize_provider_name(arg_provider or env_provider or default_provider)
    model_name = arg_model or env_model
    return ProviderConfig(provider_name=provider_name, model_name=model_name)


def _resolve_claude_cli_command(explicit: str | None) -> str:
    """Pick the real binary, not the npm `.cmd`/`.ps1` shim, on Windows.

    `subprocess.run([..], shell=False)` executing a `.cmd` file still routes
    through `cmd.exe /c` to interpret the batch script, and that layer's
    batch-argument parsing corrupts a multi-line prompt (confirmed: a prompt
    containing a blank line is silently truncated at the first blank line,
    so the model only ever sees the instructions, never the data block that
    followed). The shim's own body is a one-line passthrough to
    `node_modules/@anthropic-ai/claude-code/bin/claude.exe` -- calling that
    binary directly skips the batch layer entirely and was verified to
    reproduce the same multi-line prompt correctly.

    An explicit `HARNESS_CLAUDE_COMMAND` always wins (the caller may already
    be pointing at a working binary, including on non-Windows hosts where
    this rewrite does not apply). Falls back to the plain `"claude"` lookup
    name if no shim can be resolved, so a host without the npm layout at all
    (or a non-Windows host) is unaffected.
    """
    if explicit:
        return explicit
    if os.name != "nt":
        return "claude"
    import shutil
    shim = shutil.which("claude.cmd") or shutil.which("claude")
    if not shim:
        return "claude"
    shim_path = Path(shim)
    if shim_path.suffix.lower() not in (".cmd", ".bat", ".ps1", ""):
        return shim
    candidate = (shim_path.parent / "node_modules" / "@anthropic-ai"
                 / "claude-code" / "bin" / "claude.exe")
    return str(candidate) if candidate.exists() else shim


def build_provider(
    config: ProviderConfig | None = None,
    *,
    env: Mapping[str, str] | None = None,
    root: Path = ROOT,
    fixture_responses: Mapping[str, str] | None = None,
):
    source_env = env if env is not None else os.environ
    selected = config or parse_provider_config(env=source_env)
    provider_name = _normalize_provider_name(selected.provider_name)

    if provider_name == "openrouter":
        return OpenRouterProvider(model_name=selected.model_name, env=source_env)
    if provider_name == "claude-cli":
        return ClaudeCliProvider(
            model_name=selected.model_name,
            root=root,
            command=_resolve_claude_cli_command(
                source_env.get("HARNESS_CLAUDE_COMMAND")),
        )
    if provider_name == "codex-cli":
        return CodexCliProvider(
            model_name=selected.model_name,
            root=root,
            command=source_env.get("HARNESS_CODEX_COMMAND") or "codex",
            env=source_env,
        )
    if provider_name == "anthropic-api":
        return AnthropicApiProvider(model_name=selected.model_name, env=source_env)
    if provider_name == "openai-api":
        return OpenAIApiProvider(model_name=selected.model_name, env=source_env)
    if provider_name == "fixture":
        return FixtureProvider(model_name=selected.model_name, responses=fixture_responses)
    raise ProviderConfigError(f"unsupported provider {selected.provider_name!r}")


def preflight(specs, *, env: Mapping[str, str] | None = None, root: Path = ROOT) -> list[str]:
    """Try to BUILD each role, and report the ones that cannot be built.

    `specs` is an iterable of `(label, provider_name, model_name)`; the caller
    resolves those the way the role's own code path resolves them, because that
    resolution differs per role and asking one generic question would give false
    assurance for a partially configured environment.

    Construction is local -- no network call, no provider round trip -- so this
    costs nothing and turns "credentials or model missing" from a failure
    partway through a paid run into a refusal before the first call. Only
    ProviderConfigError is caught: that is the "cannot be configured" class. A
    genuine execution failure is not a preflight matter and must surface where
    it happens.

    Returns a list of human-readable failures; empty means every role built.
    """
    source_env = os.environ if env is None else env
    failures: list[str] = []
    for label, provider_name, model_name in specs:
        try:
            build_provider(
                ProviderConfig(provider_name or DEFAULT_PROVIDER, model_name),
                env=source_env, root=root)
        except ProviderConfigError as exc:
            failures.append(f"{label}: {exc}")
    return failures


def _normalize_provider_name(provider_name: str) -> str:
    normalized = provider_name.strip().lower()
    if normalized not in SUPPORTED_PROVIDERS:
        raise ProviderConfigError(
            f"unsupported provider {provider_name!r}; expected one of {', '.join(SUPPORTED_PROVIDERS)}"
        )
    return normalized


def _image_data_url(image_path: Path) -> str:
    path = Path(image_path)
    mime_type = mimetypes.guess_type(str(path))[0] or "image/png"
    try:
        raw = path.read_bytes()
    except OSError as exc:
        # A missing/unreadable page image must surface as a clean provider error,
        # not a raw FileNotFoundError traceback out of the caller.
        raise ProviderExecutionError(f"could not read image {path}: {exc}") from exc
    return f"data:{mime_type};base64,{base64.b64encode(raw).decode('ascii')}"


def _image_content_parts(image_paths: Sequence[Path]) -> list[dict[str, Any]]:
    return [
        {"type": "input_image", "image_url": _image_data_url(Path(p)), "detail": "high"}
        for p in image_paths
    ]


def _extract_openai_output_text(response: Mapping[str, Any]) -> str | None:
    if isinstance(response.get("output_text"), str):
        return response["output_text"]

    parts: list[str] = []
    for item in response.get("output", []) or []:
        if not isinstance(item, Mapping):
            continue
        for content in item.get("content", []) or []:
            if not isinstance(content, Mapping):
                continue
            if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                parts.append(content["text"])
    if parts:
        return "".join(parts)
    return None


def _chat_image_content_parts(image_paths: Sequence[Path]) -> list[dict[str, Any]]:
    """Image parts in the OpenAI *chat-completions* shape.

    Distinct from `_image_content_parts`, which builds the *Responses* API
    shape (`input_image` with a bare string url). The two are not
    interchangeable: a chat-completions endpoint rejects `input_image`, and the
    silent version of that mistake is a request that carries no image at all.
    """
    return [
        {"type": "image_url", "image_url": {"url": _image_data_url(Path(p))}}
        for p in image_paths
    ]


def _openrouter_error_body(text: str) -> str:
    """A server diagnostic, bounded, for an exception message.

    A 400 can quote the offending request back, and the offending request is a
    page of claim material. The message has to stay diagnosable without turning
    every provider error into an unredacted document dump in a terminal log.
    """
    if len(text) <= _OPENROUTER_ERROR_BODY_MAX_CHARS:
        return text
    return (text[:_OPENROUTER_ERROR_BODY_MAX_CHARS]
            + f"... [{len(text) - _OPENROUTER_ERROR_BODY_MAX_CHARS} more chars omitted]")


def _require_transport_security(base_url: str, env: Mapping[str, str]) -> None:
    """Refuse a base URL that would send the API key in clear.

    https always passes. http passes only for a loopback host -- the loopback
    test server, or a proxy on the same machine, neither of which puts anything
    on a network. Any other scheme is refused outright rather than attempted.
    """
    parts = urllib.parse.urlsplit(base_url)
    if parts.scheme == "https":
        return
    if str(env.get(_OPENROUTER_INSECURE_OPT_OUT_ENV, "")).strip() == "1":
        return
    host = (parts.hostname or "").lower()
    if parts.scheme == "http" and host in _LOOPBACK_HOSTS:
        return
    raise ProviderConfigError(
        f"openrouter base URL {base_url!r} is not https. The API key and the "
        "case material would travel in clear. Use https, or set "
        f"{_OPENROUTER_INSECURE_OPT_OUT_ENV}=1 to accept that deliberately."
    )


def _openrouter_message_text(message: Mapping[str, Any]) -> str | None:
    """Assistant text from a chat-completions message.

    `content` is normally a string, but a multimodal-output model can return
    the OpenAI content-part list instead; joining the text parts covers both
    without the caller needing to know which model served the request.
    """
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            part["text"] for part in content
            if isinstance(part, Mapping) and isinstance(part.get("text"), str)
        ]
        if parts:
            return "".join(parts)
    return None


def _openrouter_tool_arguments(
    message: Mapping[str, Any], tool_name: str
) -> dict[str, Any] | None:
    """The forced tool call's arguments, as a mapping, or None.

    `function.arguments` is a JSON *string* in the OpenAI contract, but some
    upstream providers hand OpenRouter a decoded object and it passes through
    as one. Accept both; anything else (including valid JSON that is not an
    object) is not a usable structured result and returns None so the caller
    fails closed.
    """
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return None
    for call in tool_calls:
        if not isinstance(call, Mapping):
            continue
        function = call.get("function")
        if not isinstance(function, Mapping) or function.get("name") != tool_name:
            continue
        arguments = function.get("arguments")
        if isinstance(arguments, Mapping):
            return dict(arguments)
        if isinstance(arguments, str):
            try:
                decoded = json.loads(arguments)
            except json.JSONDecodeError:
                return None
            if isinstance(decoded, dict):
                return decoded
        return None
    return None
