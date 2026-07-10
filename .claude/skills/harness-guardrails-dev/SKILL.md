---
name: harness-guardrails-dev
description: Dev-phase-only hard constraints for the loss-adjustment harness PoC — ground-truth isolation, per-file intake review, and the dev/prod file-naming convention. These rules exist only because a ground-truth answer key sits in this repo during evaluation; they stop applying once there's no ground truth to isolate. Every agent must follow this during the PoC phase, in addition to harness-guardrails (the always-on prod rules).
---

# Harness Guardrails — Dev Phase

These rules are scoped to the PoC/evaluation phase specifically — they exist because a ground-truth answer key sits in the same repo as the model's inputs, which is not a permanent production condition. See `harness-guardrails` for the rules that apply regardless of dev or production.

## D1. Ground-truth isolation

No agent reads `source-cases/` final reports or `data/ground_truth/` — with exactly one exception: the evaluation stage, and only after human review is complete. If any agent (including the evaluation stage, outside its designated moment) is found to have accessed ground truth: halt the run immediately, exclude that run's outputs from evaluation entirely.

## D2. Intake requires a per-file review ledger

At intake, every file in a case gets an entry in `_source_ledger.json` recording its classification (raw / ground_truth) and a review status: `pending`, `approved`, or `rejected`. Every file starts `pending`. A human must review the classification and set every file to `approved` before any copying happens — the intake tool will not execute while any file remains `pending`.

If a human marks a file `rejected` (classification looks wrong), intake halts for the **entire case** — no file copies, not even the ones already approved — until the rejected file is resolved. Review status lives only in `_source_ledger.json`; no file's status is inferred from anywhere else, so nothing can be mistaken for reviewed when it isn't.

## D3. Dev/prod file-naming convention

Any file whose contents apply only during the PoC/evaluation dev phase (not valid once there's no ground truth to isolate) is suffixed `.dev` before its final extension — e.g. `taxonomy.dev.json`, `notes.dev.md`.

For skills: the containing folder gets a `-dev` suffix (e.g. `harness-guardrails-dev/`), and any non-entry files inside also get `.dev` suffixed. The one exception is the skill's entry file, which stays literally `SKILL.md` — required for the harness to discover it — with the folder name alone carrying the marker.

This convention is itself dev-only guidance — it stops mattering once there's no dev/prod split left to track.

## D4. Directory/stage references must stay in sync with reality

Skill and agent docs that name specific directories or pipeline stages must stay in sync with the real structure. When the project's directory structure or stage names change, every doc referencing the old path/name gets updated in the same change — not left stale for someone to trip over later. If a stale reference is found (a skill says one thing, reality is another), that mismatch gets fixed immediately, not noted and deferred.
