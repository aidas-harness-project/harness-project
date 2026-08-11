"""T6: the process-wide provider in-flight cap.

The cap exists because the pools nest -- page workers already run 4-8 wide and
document workers (T8) multiply that. Two properties matter and are tested by
OBSERVING concurrency, not by reading the limit back:

1. the observed maximum never exceeds the configured cap, and
2. a nested pool (documents x pages) does not deadlock.

(2) is the one that would be catastrophic and silent: if the permit were held
around a whole P8 chain instead of the leaf call, a worker would wait for a
permit while holding one, and the run would hang rather than fail.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import llm_providers as lp


@pytest.fixture(autouse=True)
def _clean_semaphore(monkeypatch):
    lp.reset_inflight_semaphore()
    yield
    lp.reset_inflight_semaphore()


class _Probe:
    """Counts concurrent occupants of provider_slot."""

    def __init__(self):
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def call(self, hold=0.02):
        with lp.provider_slot("test"):
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            time.sleep(hold)
            with self.lock:
                self.active -= 1


def test_observed_concurrency_never_exceeds_the_cap(monkeypatch):
    monkeypatch.setenv(lp.LLM_MAX_INFLIGHT_ENV, "3")
    lp.reset_inflight_semaphore()
    probe = _Probe()
    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(lambda _: probe.call(), range(24)))
    assert probe.peak <= 3, f"cap 3 exceeded: observed {probe.peak}"


def test_the_cap_is_actually_reached(monkeypatch):
    """A cap that is never reached would make the test above pass vacuously."""
    monkeypatch.setenv(lp.LLM_MAX_INFLIGHT_ENV, "4")
    lp.reset_inflight_semaphore()
    probe = _Probe()
    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(lambda _: probe.call(0.05), range(24)))
    assert probe.peak == 4


def test_nested_pools_do_not_deadlock(monkeypatch):
    """The trap the plan names: documents x pages, with the cap SMALLER than
    the number of document workers. If a permit were ever held across a chain
    that itself acquires permits, this hangs instead of finishing."""
    monkeypatch.setenv(lp.LLM_MAX_INFLIGHT_ENV, "2")
    lp.reset_inflight_semaphore()
    probe = _Probe()

    def document_worker(_):
        # A document worker holds NO permit itself; it only spawns page
        # workers whose leaf calls take permits. This mirrors run_ocr.
        with ThreadPoolExecutor(max_workers=4) as pages:
            list(pages.map(lambda _: probe.call(0.01), range(4)))
        return True

    with ThreadPoolExecutor(max_workers=3) as docs:
        futures = [docs.submit(document_worker, i) for i in range(3)]
        done = [f.result(timeout=30) for f in futures]

    assert all(done)
    assert probe.peak <= 2


def test_p8_chain_shape_does_not_self_deadlock(monkeypatch):
    """reader_a -> reader_b -> compare runs sequentially inside ONE page
    worker. With the cap at 1, that chain must still complete: each leaf takes
    and releases a permit in turn."""
    monkeypatch.setenv(lp.LLM_MAX_INFLIGHT_ENV, "1")
    lp.reset_inflight_semaphore()
    probe = _Probe()

    def page_worker(_):
        probe.call(0.001)   # reader_a
        probe.call(0.001)   # reader_b
        probe.call(0.001)   # compare
        return True

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(page_worker, i) for i in range(8)]
        assert all(f.result(timeout=30) for f in futures)
    assert probe.peak == 1


def test_zero_disables_the_cap(monkeypatch):
    """The documented rollback: HARNESS_LLM_MAX_INFLIGHT=0 restores the
    previous unbounded behaviour with no code change."""
    monkeypatch.setenv(lp.LLM_MAX_INFLIGHT_ENV, "0")
    lp.reset_inflight_semaphore()
    assert lp._get_inflight_semaphore() is None
    probe = _Probe()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: probe.call(0.03), range(16)))
    assert probe.peak > 1, "disabling the cap should not serialise calls"


def test_a_permit_is_released_when_the_call_raises(monkeypatch):
    """A leaked permit degrades silently into a smaller cap and eventually a
    hang, so the failure path matters as much as the success path."""
    monkeypatch.setenv(lp.LLM_MAX_INFLIGHT_ENV, "1")
    lp.reset_inflight_semaphore()
    for _ in range(5):
        with pytest.raises(RuntimeError):
            with lp.provider_slot("test"):
                raise RuntimeError("boom")
    # If any permit leaked, this acquire would block forever.
    sem = lp._get_inflight_semaphore()
    assert sem.acquire(timeout=5), "a permit leaked on the exception path"
    sem.release()


@pytest.mark.parametrize("raw,expected", [
    ("", lp.DEFAULT_LLM_MAX_INFLIGHT),
    ("6", 6),
    ("abc", lp.DEFAULT_LLM_MAX_INFLIGHT),
    ("-2", 0),
    ("0", 0),
])
def test_limit_resolution(monkeypatch, raw, expected):
    monkeypatch.setenv(lp.LLM_MAX_INFLIGHT_ENV, raw)
    assert lp._resolve_max_inflight() == expected


def test_default_cap_is_sixteen(monkeypatch):
    """Raised 6 -> 16 on 2026-08-11 on measurement, not headroom.

    At 6 the cap was the binding constraint and was eating the gain it was
    meant to protect: running two documents concurrently made each document
    individually slower (CASE_953 DOC_003: 53.5s -> 87.9s) because 16 requests
    were squeezed through 6 slots. Per-document time SUM went 154.6s -> 214.4s
    at cap 6, versus 156.4s at cap 16.

    Pinned because the value is invisible at runtime -- a silent revert to 6
    shows up only as a slower run that still passes everything else.
    """
    monkeypatch.delenv(lp.LLM_MAX_INFLIGHT_ENV, raising=False)
    assert lp.DEFAULT_LLM_MAX_INFLIGHT == 16
    assert lp._resolve_max_inflight() == 16


def test_changing_the_env_rebuilds_the_semaphore(monkeypatch):
    monkeypatch.setenv(lp.LLM_MAX_INFLIGHT_ENV, "2")
    lp.reset_inflight_semaphore()
    first = lp._get_inflight_semaphore()
    monkeypatch.setenv(lp.LLM_MAX_INFLIGHT_ENV, "5")
    second = lp._get_inflight_semaphore()
    assert first is not second, "a changed cap must not keep the old semaphore"
