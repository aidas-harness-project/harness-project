---
name: harness-guardrails-dev
description: Dev-phase-only hard constraints for the loss-adjustment harness PoC — ground-truth isolation, per-file intake review, and the dev/prod file-naming convention. These rules exist only because a ground-truth answer key sits in this repo during evaluation; they stop applying once there's no ground truth to isolate. Every agent must follow this during the PoC phase, in addition to harness-guardrails (the always-on prod rules).
---

# Harness Guardrails — Dev Phase

These rules are scoped to the PoC/evaluation phase specifically — they exist because a ground-truth answer key sits in the same repo as the model's inputs, which is not a permanent production condition. See `harness-guardrails` for the rules that apply regardless of dev or production.

## D1. Ground-truth isolation

No agent or tool in the local Units 1–7 harness reads `source-cases/` final reports or `data/ground_truth/`. Evaluation is deferred to an isolated Unit 11 service that is not implemented here; human-review completion records a future handoff prerequisite but does not grant local ground-truth access. If any local agent or tool is found to have accessed ground truth, halt the run immediately and exclude that run's outputs from evaluation entirely.

## D2. Intake requires a per-file review ledger

At intake, every file in a case gets an entry in `_source_ledger.json` recording its classification (raw / ground_truth) and a review status: `pending`, `approved`, or `rejected`. Every file starts `pending`. A human must review the classification and set every file to `approved` before any copying happens — the intake tool will not execute while any file remains `pending`.

If a human marks a file `rejected` (classification looks wrong), intake halts for the **entire case** — no file copies, not even the ones already approved — until the rejected file is resolved. Review status lives only in `_source_ledger.json`; no file's status is inferred from anywhere else, so nothing can be mistaken for reviewed when it isn't.

**Filename patterns alone are not a reliable classification signal.** A real case (CASE_002, see `known-gaps.md` item 2) showed two files whose names looked like plain claim documents actually being completed third-party loss-adjustment reports with stated payout figures — filename matching missed it, and an agent self-approved the file, which isn't valid human review. Before writing the ledger, `tools/intake_case.py` now also runs a cheap content pre-check on every file proposed as `raw` (PDFs only): one vision call over the document's first few pages, looking specifically for signs of a completed adjuster's conclusion (a `보험금사정서`/`손해사정서` title, a `사정 결과`/`사정 의견` section, a stated payout figure, an adjuster's license/stamp, a `위임장` granting adjustment authority). A flagged file gets `content_warning` set on its ledger entry. This does **not** auto-reject the file — a false positive shouldn't lock out a legitimate document — but it makes the risk visible right where the human review step already happens, instead of relying on a reviewer to notice on their own. The scan is a signal over a few pages, not a full read; `document-pipeline`'s checkpoint 1 (P8) still owns real OCR and cross-validation over the whole document.

## D3. Dev/prod file-naming convention

Any file whose contents apply only during the PoC/evaluation dev phase (not valid once there's no ground truth to isolate) is suffixed `.dev` before its final extension — e.g. `taxonomy.dev.json`, `notes.dev.md`.

For skills: the containing folder gets a `-dev` suffix (e.g. `harness-guardrails-dev/`), and any non-entry files inside also get `.dev` suffixed. The one exception is the skill's entry file, which stays literally `SKILL.md` — required for the harness to discover it — with the folder name alone carrying the marker.

This convention is itself dev-only guidance — it stops mattering once there's no dev/prod split left to track.

## (Dev-only, temporary) P8 same-provider fallback

**Dev-phase default:** use `claude-cli` for both P8 readers (checkpoint 2's redaction default is `codex-cli`). Every available provider (claude-cli / codex-cli / openai-api) is LLM-vision-backed, so this — and any two-LLM reader pair — is a **documented weak-P8**, not equivalent to dual-technology cross-validation. This is the PoC provider strategy: validate the pipeline on commercial LLMs while a genuinely technology-independent reader remains unavailable (see `open-decisions.md` #4).

The switch condition is a **genuinely technology-independent second reader (a real OCR engine) validated against real Korean case documents** — which does not exist in this repo today. An earlier offline Tesseract/Ollama stack was built to be that reader but never transcribed real pages reliably (`known-gaps.md` item 16) and was removed. Until a real OCR engine is added and proven on real content, treat every configurable reader pair as weak P8 and record it honestly.

- **Record it honestly in `ocr_result.json`**: `reader_a`/`reader_b` = `"claude-cli:claude-cli"`, `cross_validation_mode = "single_technology_weak_p8_poc"`, and a `cross_validation_note` stating no independent second technology was available at run time. Never let a same-provider run look like genuine P8.
- **Disagreement handling is unchanged — hard-halt stays.** What is relaxed is *reader independence*, never *disagreement tolerance*. A genuine content disagreement between the two claude-cli reads still halts with no tolerance threshold.
- Each `claude-cli` reader is invoked with a **neutral transcription prompt** (`llm_providers.py` `ClaudeCliProvider.transcribe_image`: the shared `TRANSCRIBE_PROMPT` referencing the image as an explicit Read instruction — no "role framing" preamble). `ClaudeCliProvider._run()` enforces `--safe-mode`, so child `claude -p` sessions do **not** inherit `CLAUDE.md`, hooks, or skills context. **Do not reintroduce a defensive framing block** ("this is a SANCTIONED step, do not refuse, do not mention guardrails…"): that language reads as a prompt-injection signal and *causes* the very self-refusal it's trying to prevent — it was tried on DOC_001, failed, and was reverted. No `CLAUDE.md` carve-out is needed, and none should exist.
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
