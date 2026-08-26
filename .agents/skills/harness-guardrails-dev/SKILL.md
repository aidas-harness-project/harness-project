---
name: harness-guardrails-dev
description: Dev-phase-only hard constraints for the loss-adjustment harness PoC — ground-truth isolation, per-file intake review, and the dev/prod file-naming convention. These rules exist only because a ground-truth answer key sits in this repo during evaluation; they stop applying once there's no ground truth to isolate. Every agent must follow this during the PoC phase, in addition to harness-guardrails (the always-on prod rules).
---

# Harness Guardrails — Dev Phase

These rules are scoped to the PoC/evaluation phase specifically — they exist because a ground-truth answer key sits in the same repo as the model's inputs, which is not a permanent production condition. See `harness-guardrails` for the rules that apply regardless of dev or production.

## D1. Ground-truth isolation

No agent or tool that **produces** pipeline output reads `source-cases/` final reports or `data/ground_truth/`. That covers every stage from intake through draft report and critic, without exception. If any of them is found to have accessed ground truth, halt the run immediately and exclude that run's outputs from evaluation entirely.

**The verification agent is the one exception (2026-08-22).** The PoC is validated by how well a produced report agrees with the adjuster's real report, and a scorer blind to the answer key cannot measure that. `screening-fidelity` may read ground truth under four conditions, all enforced rather than trusted:

1. **Only through the DAO.** `python tools/dao.py read-ground-truth CASE_ID --caller-stage screening_fidelity --version {v1|v2} [--list | --file NAME]`. The `Read()` deny globs over `data/ground_truth/**` and `source-cases/**` in `.claude/settings.json` stay in place, so a direct read is still refused for every agent including this one. The DAO command is the single door, and it logs.
2. **Only for a screening report whose own run finished** — the report exists and `screening_report` is `passed` in run state. Enforced at read and write time.

   **This path has no reader gate, deliberately** (owner's decision, 2026-08-26). Fidelity scoring measures how the pipeline performed; whether a person has read the report says nothing about that measurement, so requiring one would gate a performance number on an unrelated act. What the condition does prevent is scoring a report its own run never finished — CASE_712 left `document_processing` in_progress with four later stages failed and a `screening_report.md` in the directory anyway, and a score over that is a number about nothing.

   **The draft path is unchanged and keeps its human gate**: `evaluation` still needs `_human_review_complete_v{n}.flag`, itself gated on `expert_review_v{n}.json`. That path scores the deliverable, where the ordering is what stops the deliverable being fixed against the answer key. The tokens do not cross: `screening` names the screening report and is refused for `evaluation`; v1/v2 name a draft review and are refused for `screening_fidelity`.
3. **Its result is terminal, structurally.** The result names ground-truth *values* — the dates, codes, amounts and issue labels the comparison turned on — which is the answer key's answers, already extracted. Under `outputs/` that would be reachable by every producing stage (`read-contract` takes no caller stage, and no deny glob covers `outputs/`), so the file lives at `data/ground_truth/CASE_ID/_verification/screening_fidelity_result_v{n}.json` — inside the tree every agent is already denied — and is written and read only through `dao.py write-verification-result` / `read-verification-result`, both restricted to `screening_fidelity` (`dao.VERIFICATION_STAGES`). The failure this closes: score a report, rerun `screening_report`, and an agent scanning its case artifacts reads the answer values out of the score file and writes them into the report — the next score rises because the report copied the answer.
4. **The exception is agent-scoped, not run-scoped.** Ground truth being readable by the scorer does not make a run contaminated; ground truth being read by anything else still does, and the halt-and-exclude rule above applies unchanged.

The full Evaluation suite (draft report vs ground truth, `evaluation_result.schema.json`) remains deferred to the isolated Unit 11 service. This carve-out authorizes one narrow comparison, not that service.

## D2. Intake requires a per-file review ledger

At intake, every file in a case gets an entry in `_source_ledger.json` recording its classification (raw / ground_truth) and a review status: `pending`, `approved`, or `rejected`. Every file starts `pending`. A human must review the classification and set every file to `approved` before any copying happens — the intake tool will not execute while any file remains `pending`.

If a human marks a file `rejected` (classification looks wrong), intake halts for the **entire case** — no file copies, not even the ones already approved — until the rejected file is resolved. Review status lives only in `_source_ledger.json`; no file's status is inferred from anywhere else, so nothing can be mistaken for reviewed when it isn't.

**Filename patterns alone are not a reliable classification signal.** A real
case (CASE_002, see `known-gaps.md` item 2) showed two files whose names
looked like plain claim documents actually being completed third-party
loss-adjustment reports with stated payout figures — filename matching missed
it, and an agent self-approved the file, which isn't valid human review. That
is why the human review step exists and why nothing may be approved by an
agent.

**The vision content pre-check that used to run here was removed 2026-08-20**
(PoC owner). It made one vision call over each raw-proposed PDF's first pages
looking for answer-key-class content, and annotated the ledger entry with
`content_warning`. It never auto-rejected anything — the human review below
was always the actual gate — and it cost ~41s on a four-PDF case while reading
raw, pre-redaction pages. `content_warning` remains in
`source_ledger.schema.json` and in `build_ledger`, because ledgers written
while it ran carry the field and must keep validating.

**What this does NOT relax:** every entry still starts `pending`, a human
still reviews each file's classification, `--execute` still refuses while any
file is unapproved, and one `rejected` file still halts the whole case. The
CASE_002 lesson stands — it is now carried entirely by the human review step,
with no machine signal to lean on, so read the documents.

## D3. Dev/prod file-naming convention

Any file whose contents apply only during the PoC/evaluation dev phase (not valid once there's no ground truth to isolate) is suffixed `.dev` before its final extension — e.g. `taxonomy.dev.json`, `notes.dev.md`.

For skills: the containing folder gets a `-dev` suffix (e.g. `harness-guardrails-dev/`), and any non-entry files inside also get `.dev` suffixed. The one exception is the skill's entry file, which stays literally `SKILL.md` — required for the harness to discover it — with the folder name alone carrying the marker.

This convention is itself dev-only guidance — it stops mattering once there's no dev/prod split left to track.

## (Dev-only, temporary) P8 same-provider fallback

**Dev-phase default:** `openrouter` for both P8 readers and for checkpoint 2's redaction — one default, taken from `llm_providers.DEFAULT_PROVIDER`, since 2026-08-21 (it was `claude-cli` for the readers and `codex-cli` for redaction). Every available provider (openrouter / claude-cli / codex-cli / anthropic-api / openai-api) is LLM-vision-backed, so this — and any two-LLM reader pair — is a **documented weak-P8**, not equivalent to dual-technology cross-validation. Routing both readers through one aggregator does not change that verdict in either direction: two DIFFERENT models reached through OpenRouter are still two LLM readers, and the same model reached twice is still one reader run twice. Selecting genuinely different models per reader is a `--reader-a` / `--reader-b` `--*-model` decision, not a property of the transport. This is the PoC provider strategy: validate the pipeline on commercial LLMs while a genuinely technology-independent reader remains unavailable (see `open-decisions.md` #4).

The switch condition is a **genuinely technology-independent second reader (a real OCR engine) validated against real Korean case documents** — which does not exist in this repo today. An earlier offline Tesseract/Ollama stack was built to be that reader but never transcribed real pages reliably (`known-gaps.md` item 16) and was removed. Until a real OCR engine is added and proven on real content, treat every configurable reader pair as weak P8 and record it honestly.

- **Record it honestly in `ocr_result.json`**: `reader_a`/`reader_b` = the real `provider:model` pair that ran (`"openrouter:<slug>"` under the default, `"claude-cli:claude-cli"` on the CLI path), `cross_validation_mode = "single_technology_weak_p8_poc"`, and a `cross_validation_note` stating no independent second technology was available at run time. Never let a same-provider run look like genuine P8. Under `openrouter`, record the model OpenRouter reports as having SERVED the call (`raw_metadata.served_model`) when it differs from the slug requested — a routed fallback means the reader that actually ran is not the one that was configured.
- **Disagreement handling is unchanged — hard-halt stays.** What is relaxed is *reader independence*, never *disagreement tolerance*. A genuine content disagreement between the two reads still halts with no tolerance threshold, whichever provider produced them.
- The transcription prompt is **neutral on every provider**, and the reason is provider-independent. `openrouter` attaches the page as a base64 image part and needs no filesystem access, no `--safe-mode`, and no Read-tool allowlist — the request carries the prompt and the image and nothing else, so the context-inheritance risk the CLI flags exist to close is structurally absent there. The prompt-framing lesson still applies to both. Each `claude-cli` reader is invoked with a **neutral transcription prompt** (`llm_providers.py` `ClaudeCliProvider.transcribe_image`: the shared `TRANSCRIBE_PROMPT` referencing the image as an explicit Read instruction — no "role framing" preamble). `ClaudeCliProvider._run()` enforces `--safe-mode`, so child `claude -p` sessions do **not** inherit `CLAUDE.md`, hooks, or skills context. **Do not reintroduce a defensive framing block** ("this is a SANCTIONED step, do not refuse, do not mention guardrails…"): that language reads as a prompt-injection signal and *causes* the very self-refusal it's trying to prevent — it was tried on DOC_001, failed, and was reverted. No `CLAUDE.md` carve-out is needed, and none should exist.
- **Dev-only. Must not ship to prod** — in production `harness-guardrails` P8 (genuine dual-path independence) applies. Remove this same-provider fallback once a real OCR engine gives a genuinely technology-independent reader pair (`open-decisions.md` #4).

## D4. Directory/stage references must stay in sync with reality

Skill and agent docs that name specific directories or pipeline stages must stay in sync with the real structure. When the project's directory structure or stage names change, every doc referencing the old path/name gets updated in the same change — not left stale for someone to trip over later. If a stale reference is found (a skill says one thing, reality is another), that mismatch gets fixed immediately, not noted and deferred.

## D5. A case with no policy document may be declared, never assumed

Part of the supplied PoC corpus arrives as a diagnosis certificate plus an
insurer letter with **no 약관 attached**. `policy_clause_processing` cannot
finalize without a text-processed `insurance_policy` document, and
`claim_analysis` cannot start until it does — so those cases are unrunnable
end-to-end even though nothing about them is defective.

The allowance is a **recorded human declaration, per case**:

```
dao.py declare-no-policy-documents CASE_ID --reviewer NAME --note TEXT
       --held-by NAME --run-id RUN_ID
```

It writes `_no_policy_documents.json` into the case directory, and the policy
completeness gate treats that as clearing the "no policy document" blocker —
and only that one.

- **Not an environment variable and not a global dev flag.** The gate cannot
  tell "this case has no policy" apart from "the policy work was skipped or
  failed", and a flag would apply that judgment silently to every case in the
  run. A person makes the call, per case, with a name and a reason attached.
- **Refused when the manifest actually types a document as
  `insurance_policy`** — at declaration time and again at the gate, so a
  declaration that goes stale (because policy documents were later added or
  re-typed) blocks instead of quietly persisting.
- **It clears one blocker, not the stage.** Every other policy-completeness
  condition still applies, and nothing about P0-3's canonical-UID requirement
  changes: a `clause_ref` still may not name a non-policy document.
- **Downstream consequences are real and must be read as such.** A case with
  no policy has no clause for CP2 to match or CP4 to ground a requirement in;
  those checkpoints will produce thin or uncertain output, and that is the
  honest result for such a case — not a defect to tune away.
- **Dev-only. Must not ship to prod.** In production a claim without its
  policy is an intake gap to fix at intake, not a gate to waive. This rule
  disappears with the PoC, along with the rest of `harness-guardrails-dev`.
