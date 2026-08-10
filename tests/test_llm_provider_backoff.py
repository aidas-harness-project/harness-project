"""claude-cli transient-failure retry: exponential backoff, full jitter, and
Retry-After handling.

Why these exist: page-level OCR concurrency means N provider calls are in
flight at once, so a shared rate limit fails them at nearly the same instant.
The previous retry slept a FIXED 2.0s, which sent all N back at exactly the
same moment -- a thundering herd against the limit that had just rejected them.
Raising DEFAULT_OCR_WORKERS makes that herd bigger, so the backoff had to land
first (docs/run-notes/OCR_PARALLEL_20260810_001.md).

Each test here was checked by reintroducing the defect it guards -- reverting
to a constant sleep, dropping the jitter, ignoring Retry-After, or retrying a
non-transient failure -- and confirming it fails.
"""
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import llm_providers as providers  # noqa: E402


# --------------------------------------------------------------------------
# _parse_retry_after
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("429 rate limit, retry-after: 7", 7.0),
    ("Retry-After=12.5", 12.5),
    ("retry after 3", 3.0),
    ("RETRY-AFTER: 0", 0.0),
    ('{"retry_after": 5}', 5.0),
])
def test_parse_retry_after_reads_delta_seconds(text, expected):
    assert providers._parse_retry_after(text) == expected


@pytest.mark.parametrize("text", [
    "",
    "some other failure",
    "exit 1, no output",
    "no stdin data received in 3s",
])
def test_parse_retry_after_returns_none_without_a_hint(text):
    """No hint -> None, so the caller falls back to ordinary backoff rather
    than inventing a wait."""
    assert providers._parse_retry_after(text) is None


def test_parse_retry_after_clamps_to_cap():
    """The value comes from outside the process. An absurd one must not park a
    whole document behind a single call."""
    assert providers._parse_retry_after("retry-after: 99999") == (
        providers._CLAUDE_CLI_RETRY_CAP_SECONDS
    )


def test_parse_retry_after_rejects_negative():
    assert providers._parse_retry_after("retry-after: -5") is None


# --------------------------------------------------------------------------
# _is_rate_limited
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "HTTP 429 Too Many Requests",
    "Error: rate limit exceeded",
    "rate_limit_error",
    "model is overloaded",
    "529 overloaded_error",
    "quota exceeded for this org",
    "at capacity, try later",
])
def test_is_rate_limited_recognises_limit_signatures(text):
    assert providers._is_rate_limited(text) is True


@pytest.mark.parametrize("text", [
    "",
    "claude-cli command not found",
    "no stdin data received in 3s",  # the real CASE_003/DOC_008 hiccup
    "exit 1, no output",
    "UnicodeDecodeError",
])
def test_is_rate_limited_ignores_ordinary_failures(text):
    """A non-rate-limit failure must not be mislabelled -- it still retries,
    but nothing should read a Retry-After out of it."""
    assert providers._is_rate_limited(text) is False


# --------------------------------------------------------------------------
# _retry_delay -- the exponential curve
# --------------------------------------------------------------------------

def test_retry_delay_ceiling_doubles_per_attempt(monkeypatch):
    """With jitter pinned to its maximum, the delay is exactly the ceiling:
    base, 2*base, 4*base. Fails if the exponent is dropped (constant sleep)."""
    monkeypatch.setattr(providers.random, "uniform", lambda _lo, hi: hi)
    base = providers._CLAUDE_CLI_RETRY_BASE_SECONDS
    assert providers._retry_delay(1) == base
    assert providers._retry_delay(2) == base * 2
    assert providers._retry_delay(3) == base * 4


def test_retry_delay_never_exceeds_cap(monkeypatch):
    monkeypatch.setattr(providers.random, "uniform", lambda _lo, hi: hi)
    for attempt in range(1, 20):
        assert providers._retry_delay(attempt) <= providers._CLAUDE_CLI_RETRY_CAP_SECONDS


def test_retry_delay_is_jittered_not_constant():
    """The property that breaks the herd: repeated calls at the SAME attempt
    number must not return the same value. Fails if jitter is removed."""
    values = {providers._retry_delay(2) for _ in range(50)}
    assert len(values) > 1, "delay is deterministic -- N workers would retry in lockstep"


def test_retry_delay_full_jitter_spans_from_zero(monkeypatch):
    """Full jitter draws from [0, ceiling] -- the low end is 0, not a floor.
    A call that retries immediately is one call, not a herd."""
    monkeypatch.setattr(providers.random, "uniform", lambda lo, _hi: lo)
    assert providers._retry_delay(3) == 0.0


def test_retry_delay_spreads_concurrent_workers():
    """The behavioural point, stated as the herd it prevents: 8 callers failing
    at the same instant must not re-arrive together."""
    arrivals = [providers._retry_delay(1) for _ in range(8)]
    assert max(arrivals) - min(arrivals) > 0.1, (
        f"8 workers would re-arrive within {max(arrivals) - min(arrivals):.3f}s "
        "of each other -- that is the thundering herd this replaced"
    )


def test_retry_delay_honours_retry_after_as_a_floor():
    """A server that names a wait outranks the local curve; jitter still rides
    on top so held-back callers do not resume in lockstep."""
    detail = "429 too many requests, retry-after: 7"
    delays = [providers._retry_delay(1, detail) for _ in range(20)]
    assert all(d >= 7.0 for d in delays), "server-requested wait was not respected"
    assert len(set(delays)) > 1, "held-back callers would resume in lockstep"


def test_retry_delay_retry_after_beats_a_smaller_local_ceiling(monkeypatch):
    """attempt 1's ceiling is 2s; a Retry-After of 7 must win, not be capped
    down to the local curve."""
    monkeypatch.setattr(providers.random, "uniform", lambda lo, _hi: lo)
    assert providers._retry_delay(1, "retry-after: 7") == 7.0


# --------------------------------------------------------------------------
# Integration with the retry loop
# --------------------------------------------------------------------------

def _failing_run(calls, stderr="rate limit exceeded, retry-after: 4"):
    def fake_run(cmd, **kwargs):
        calls.append(1)
        result = mock.Mock()
        result.returncode = 1
        result.stdout = ""
        result.stderr = stderr
        return result
    return fake_run


def test_retry_loop_sleeps_with_growing_ceiling(monkeypatch, tmp_path):
    """End to end: the loop must call the jittered delay between attempts, and
    the ceiling must grow. Pins the wiring, not just the helper -- a helper
    computed but never called would pass every test above."""
    slept = []
    monkeypatch.setattr(providers.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(providers.random, "uniform", lambda _lo, hi: hi)
    calls = []
    monkeypatch.setattr(providers.subprocess, "run",
                        _failing_run(calls, stderr="transient boom"))

    provider = providers.ClaudeCliProvider(root=tmp_path)
    with pytest.raises(providers.ProviderExecutionError):
        provider.compare_text("prompt", "v1")

    assert len(calls) == providers._CLAUDE_CLI_MAX_ATTEMPTS
    # One sleep fewer than attempts: no wait after the final failure.
    assert len(slept) == providers._CLAUDE_CLI_MAX_ATTEMPTS - 1
    assert slept == sorted(slept), f"backoff did not grow: {slept}"
    assert slept[0] < slept[-1], "ceiling never doubled -- constant sleep?"


def test_retry_loop_feeds_server_detail_into_the_delay(monkeypatch, tmp_path):
    """The CLI's diagnostic must reach _retry_delay, or Retry-After is dead
    code. Fails if the loop passes no detail."""
    slept = []
    monkeypatch.setattr(providers.time, "sleep", lambda s: slept.append(s))
    calls = []
    monkeypatch.setattr(providers.subprocess, "run",
                        _failing_run(calls, stderr="429: retry-after: 9"))

    provider = providers.ClaudeCliProvider(root=tmp_path)
    with pytest.raises(providers.ProviderExecutionError):
        provider.compare_text("prompt", "v1")

    assert slept, "no sleep recorded"
    assert all(s >= 9.0 for s in slept), (
        f"server-requested 9s ignored by the loop: {slept}"
    )


def test_retry_loop_still_does_not_retry_missing_command(monkeypatch, tmp_path):
    """Regression guard: the backoff change must not make a non-transient
    failure retryable."""
    monkeypatch.setattr(providers.time, "sleep", lambda *_: None)
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(1)
        raise FileNotFoundError("claude not found")

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    provider = providers.ClaudeCliProvider(root=tmp_path)

    with pytest.raises(providers.ProviderExecutionError):
        provider.compare_text("prompt", "v1")
    assert len(calls) == 1, "a missing binary must not burn retry attempts"


def test_retry_loop_recovers_without_sleeping_after_success(monkeypatch, tmp_path):
    """A call that succeeds on attempt 2 sleeps exactly once."""
    slept = []
    monkeypatch.setattr(providers.time, "sleep", lambda s: slept.append(s))
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(1)
        result = mock.Mock()
        if len(calls) == 1:
            result.returncode = 1
            result.stdout = ""
            result.stderr = "transient"
        else:
            result.returncode = 0
            result.stdout = "recovered"
            result.stderr = ""
        return result

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    provider = providers.ClaudeCliProvider(root=tmp_path)
    result = provider.compare_text("prompt", "v1")

    assert result.text == "recovered"
    assert len(slept) == 1
    assert result.metadata()["raw_metadata"]["attempts"] == 2


# --------------------------------------------------------------------------
# T4d: codex-cli gets the same bounded retry
# --------------------------------------------------------------------------
#
# codex-cli is checkpoint 2's dev-phase default, and it had NO retry path at
# all -- the redaction loop was the least resilient path in the pipeline (the
# plan's B1). Page-level parallelism (T4c) makes a transient failure MORE
# likely, not less, because N calls now hit the backend simultaneously.


def _codex_run_factory(calls, *, returncode=1, stderr="transient boom",
                       message_text=None, raises=None):
    """Fake subprocess.run for codex-cli.

    codex-cli returns its answer via --output-last-message, so a successful
    fake has to write that file rather than return stdout.
    """
    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if raises is not None:
            raise raises
        if message_text is not None:
            out_path = Path(cmd[cmd.index("--output-last-message") + 1])
            out_path.write_text(message_text, encoding="utf-8")
        result = mock.Mock()
        result.returncode = returncode
        result.stdout = ""
        result.stderr = stderr
        return result
    return fake_run


def test_codex_retries_a_transient_failure_then_raises(monkeypatch, tmp_path):
    slept = []
    monkeypatch.setattr(providers.time, "sleep", lambda s: slept.append(s))
    calls = []
    monkeypatch.setattr(providers.subprocess, "run", _codex_run_factory(calls))

    provider = providers.CodexCliProvider(root=tmp_path)
    with pytest.raises(providers.ProviderExecutionError) as exc:
        provider.redact_text("prompt", "v1")

    assert len(calls) == providers._CLAUDE_CLI_MAX_ATTEMPTS
    assert len(slept) == providers._CLAUDE_CLI_MAX_ATTEMPTS - 1
    assert "codex-cli call failed" in str(exc.value)


def test_codex_recovers_when_a_later_attempt_succeeds(monkeypatch, tmp_path):
    """The point of the retry: a transient blip must not kill the page."""
    monkeypatch.setattr(providers.time, "sleep", lambda _s: None)
    calls = []

    def flaky(cmd, **kwargs):
        calls.append(list(cmd))
        result = mock.Mock()
        result.stderr = "temporary failure"
        result.stdout = ""
        if len(calls) < 3:
            result.returncode = 1
            return result
        Path(cmd[cmd.index("--output-last-message") + 1]).write_text(
            "REDACTED", encoding="utf-8")
        result.returncode = 0
        result.stderr = ""
        return result

    monkeypatch.setattr(providers.subprocess, "run", flaky)
    provider = providers.CodexCliProvider(root=tmp_path)
    result = provider.redact_text("prompt", "v1")

    assert result.text == "REDACTED"
    assert len(calls) == 3
    assert result.raw_metadata["attempts"] == 3


def test_codex_backoff_grows_between_attempts(monkeypatch, tmp_path):
    slept = []
    monkeypatch.setattr(providers.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(providers.random, "uniform", lambda _lo, hi: hi)
    calls = []
    monkeypatch.setattr(providers.subprocess, "run", _codex_run_factory(calls))

    provider = providers.CodexCliProvider(root=tmp_path)
    with pytest.raises(providers.ProviderExecutionError):
        provider.redact_text("prompt", "v1")
    assert slept == sorted(slept)
    assert slept[0] < slept[-1], "ceiling never doubled -- constant sleep?"


def test_codex_honours_a_server_supplied_retry_after(monkeypatch, tmp_path):
    slept = []
    monkeypatch.setattr(providers.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(providers.random, "uniform", lambda lo, _hi: lo)
    calls = []
    monkeypatch.setattr(providers.subprocess, "run",
                        _codex_run_factory(calls, stderr="429 rate limit, retry-after: 5"))

    provider = providers.CodexCliProvider(root=tmp_path)
    with pytest.raises(providers.ProviderExecutionError):
        provider.redact_text("prompt", "v1")
    assert slept and all(s >= 5.0 for s in slept), slept


def test_codex_does_not_retry_a_missing_binary(monkeypatch, tmp_path):
    """A missing binary stays missing -- retrying it burns the backoff budget
    on a condition that cannot clear. This is also the live failure on this
    machine: codex resolves to codex.CMD, which Windows CreateProcess will not
    launch by the bare name."""
    slept = []
    monkeypatch.setattr(providers.time, "sleep", lambda s: slept.append(s))
    calls = []
    monkeypatch.setattr(providers.subprocess, "run",
                        _codex_run_factory(calls, raises=FileNotFoundError()))

    provider = providers.CodexCliProvider(root=tmp_path)
    with pytest.raises(providers.ProviderExecutionError) as exc:
        provider.redact_text("prompt", "v1")
    assert "command not found" in str(exc.value)
    assert len(calls) == 1, "a missing binary must not be retried"
    assert slept == []


def test_codex_timeout_becomes_a_provider_error_and_is_retried(monkeypatch, tmp_path):
    """Previously a TimeoutExpired escaped raw: callers catching
    ProviderExecutionError saw an unexpected exception class instead."""
    monkeypatch.setattr(providers.time, "sleep", lambda _s: None)
    calls = []
    monkeypatch.setattr(
        providers.subprocess, "run",
        _codex_run_factory(calls, raises=providers.subprocess.TimeoutExpired("codex", 5)))

    provider = providers.CodexCliProvider(root=tmp_path)
    with pytest.raises(providers.ProviderExecutionError) as exc:
        provider.redact_text("prompt", "v1")
    assert "timed out" in str(exc.value)
    assert len(calls) == providers._CLAUDE_CLI_MAX_ATTEMPTS


def test_codex_empty_output_is_retried_then_fails_closed(monkeypatch, tmp_path):
    """An empty last-message file is the shape a truncated run takes, so it is
    retried -- but it must never be returned as content."""
    monkeypatch.setattr(providers.time, "sleep", lambda _s: None)
    calls = []
    monkeypatch.setattr(providers.subprocess, "run",
                        _codex_run_factory(calls, returncode=0, stderr="",
                                           message_text="   "))

    provider = providers.CodexCliProvider(root=tmp_path)
    with pytest.raises(providers.ProviderExecutionError) as exc:
        provider.redact_text("prompt", "v1")
    assert "empty output" in str(exc.value)
    assert len(calls) == providers._CLAUDE_CLI_MAX_ATTEMPTS


def test_codex_scratch_file_is_cleaned_up_after_exhausted_retries(monkeypatch, tmp_path):
    monkeypatch.setattr(providers.time, "sleep", lambda _s: None)
    calls = []
    monkeypatch.setattr(providers.subprocess, "run", _codex_run_factory(calls))
    provider = providers.CodexCliProvider(root=tmp_path)
    with pytest.raises(providers.ProviderExecutionError):
        provider.redact_text("prompt", "v1")
    leftovers = list((tmp_path / "_ocr_scratch").glob("codex-last-message-*"))
    assert leftovers == [], f"temp files leaked across retries: {leftovers}"
