"""Boundary judging: one pass, and a failed call is never silently a verdict.

Found by running Stage 2 cold on CASE_961 (2026-08-12). Nine consecutive
boundary-judge calls failed inside ~0.05s each -- far too fast to be real model
calls -- and the run produced 13 segments where CASE_911 produced 11 (p4/p5 and
p18/p19 each over-split), with `undecided_pages: []` recorded on the proposal.

Two separate defects combined to make that invisible:

1. `_judge_boundary` caught every exception and returned None, the same value
   it returns for "the model answered but not usably". The caller's response --
   split, and flag the page -- is right for both, but nothing anywhere could
   distinguish "this bundle is ambiguous" from "the provider was broken for
   this entire run".

2. `propose` called `processed_text_boundaries` and then
   `processed_undecided_pages` as two INDEPENDENT passes over the same pages.
   That re-judged every ambiguous page (exactly 2x the cost) and, worse, let
   the two passes disagree: the boundaries came from one set of verdicts and
   the flags from another, so `undecided_pages` did not describe the boundaries
   actually written.

Over-splitting is the deliberately safe direction -- a human merges two
segments at the approval gate, whereas over-merging fuses two documents into
one `document_type` and propagates downstream. But that safety depends
entirely on the gate being able to SEE it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import segment_case as sc  # noqa: E402


class _Judge:
    """Counts calls; optionally starts failing after N of them."""

    def __init__(self, fail_after: int | None = None, verdict: bool = True):
        self.calls = 0
        self.fail_after = fail_after
        self.verdict = verdict

    def classify_document(self, prompt, version):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise RuntimeError("simulated provider failure")

        class _R:
            text = ('{"starts_new_document": %s, "confidence": 0.9}'
                    % ("true" if self.verdict else "false"))
        return _R()


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    """A 4-page medical bundle with two pages the title rules cannot settle.

    `REPORT` names a form KIND, not a document, so the deterministic rule
    hands those pages to the judge -- which is what makes them usable here.
    """
    d = tmp_path / "data" / "processed" / "CASE_900" / "DOC_001"
    d.mkdir(parents=True)
    pages = ["진 단 서\n환자", "REPORT\n소견", "REPORT\n계속", "진료비 세부산정내역\n항목"]
    (d / "redacted_text.md").write_text(
        "".join(f"<<<PAGE page={n}>>>\n{t}\n" for n, t in enumerate(pages, 1)),
        encoding="utf-8")
    monkeypatch.setattr(sc, "ROOT", tmp_path)
    return tmp_path


class TestSinglePass:
    def test_boundaries_and_flags_come_from_one_pass(self, bundle) -> None:
        judge = _Judge()
        boundaries, undecided = sc.processed_boundaries_with_undecided(
            "CASE_900", "DOC_001", 4, judge=judge)
        assert boundaries is not None
        assert undecided == []
        assert judge.calls == 2, (
            "each ambiguous page must be judged exactly once; the old two-call "
            "path judged every one of them twice"
        )

    def test_two_call_path_costs_twice_as_much(self, bundle) -> None:
        """Pins the regression this replaced, so it cannot quietly come back."""
        judge = _Judge()
        sc.processed_text_boundaries("CASE_900", "DOC_001", 4, judge=judge)
        after_boundaries = judge.calls
        sc.processed_undecided_pages("CASE_900", "DOC_001", 4, judge=judge)
        assert judge.calls == after_boundaries * 2

    def test_no_judge_still_returns_boundaries(self, bundle) -> None:
        boundaries, undecided = sc.processed_boundaries_with_undecided(
            "CASE_900", "DOC_001", 4, judge=None)
        assert boundaries is not None
        assert undecided == []

    def test_missing_processed_text_returns_none(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(sc, "ROOT", tmp_path)
        assert sc.processed_boundaries_with_undecided(
            "CASE_900", "DOC_404", 4, judge=_Judge()) == (None, [])


class TestFailuresAreVisible:
    def test_a_failed_call_is_recorded_on_the_judge(self, bundle) -> None:
        judge = _Judge(fail_after=0)
        _, undecided = sc.processed_boundaries_with_undecided(
            "CASE_900", "DOC_001", 4, judge=judge)
        failures = sc.judge_failures(judge)
        assert failures, "a provider exception must leave a trace"
        assert "RuntimeError" in failures[0]
        assert undecided, "a page whose judge call failed is still undecided"

    def test_a_successful_run_records_no_failures(self, bundle) -> None:
        judge = _Judge()
        sc.processed_boundaries_with_undecided(
            "CASE_900", "DOC_001", 4, judge=judge)
        assert sc.judge_failures(judge) == []

    def test_failures_do_not_crash_on_a_judge_that_rejects_attributes(self) -> None:
        """Recording is best-effort: the fail-toward-splitting behaviour is
        still correct without the diagnostic, so a judge with __slots__ must
        not turn a swallowed provider error into a crash."""
        class Slotted:
            __slots__ = ()

            def classify_document(self, prompt, version):
                raise RuntimeError("boom")

        assert sc._judge_boundary("a", "b", Slotted()) is None
        assert sc.judge_failures(Slotted()) == []

    def test_undecided_is_empty_when_the_policy_rule_wins(self, monkeypatch) -> None:
        """Flags must describe the boundaries actually RETURNED.

        Both rules run over the same pages and the one finding MORE boundaries
        wins. The policy rule never consults the judge, so when it wins, any
        page the medical pass could not settle belongs to a boundary set that
        was discarded -- reporting it would send a reviewer to a page the
        returned boundaries never left undecided.

        Driven through boundaries_from_page_texts directly with a stub for each
        rule: constructing real page text where policy out-counts medical AND
        medical still consults the judge is fiddly, and stubbing states the
        actual precondition instead of hoping a fixture produces it. (A first
        attempt used real text and asserted the wrong thing -- the medical rule
        won there, 4 boundaries to 3, so its flags were correct.)
        """
        calls = {"n": 0}

        def fake_rule(lines, *, medical=False, judge=None, page_texts=None,
                      undecided=None):
            calls["n"] += 1
            if medical:
                if undecided is not None:
                    undecided.extend([2, 3])
                return {1: "med"}                 # fewer -> loses
            return {1: "pol", 2: "pol", 3: "pol"}  # more -> wins

        # monkeypatch rather than assign-and-restore: it undoes the patch even
        # if the call below raises, so a failure here can never leave the module
        # global stubbed for whichever test the randomizer runs next.
        monkeypatch.setattr(sc, "_boundaries_from_page_lines", fake_rule)

        collected: list[int] = []
        boundaries = sc.boundaries_from_page_texts(
            ["a", "b", "c"], medical="auto", judge=_Judge(),
            undecided=collected)

        # Both rules must actually have gone through the stub. Asserted because
        # the interesting assertions below are all about which rule WON, and a
        # real rule reaching them instead would fail them for a reason that has
        # nothing to do with the behaviour under test.
        assert calls["n"] == 2, (
            f"expected both rules to run through the stub, saw {calls['n']} "
            "call(s) -- boundaries_from_page_texts is not calling "
            "_boundaries_from_page_lines the way this test assumes"
        )
        assert boundaries == {1: "pol", 2: "pol", 3: "pol"}
        assert collected == [], (
            "the policy rule won and never asked the judge, so nothing it "
            "returned was left undecided"
        )
