---
name: policy-document-collection-audit
description: "Use when collecting, refreshing, classifying, auditing, or reviewing official Korean insurance policy documents under docs/policy. Enforce source admission, provenance, currentness, taxonomy, PDF-integrity, and coverage-gap rules."
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [insurance, policy-documents, provenance, pdf-audit, source-grounding]
    related_skills: []
---

# Official Policy Document Collection and Audit

## Overview

`docs/policy/` is a source corpus of Korean insurer policy and terms documents (`보험약관`). Its purpose is clause retrieval and research, not legal advice. The reliable unit is not merely a downloaded PDF: it is a PDF plus an insurer-controlled source URL, landing page, retrieval record, normalized primary type, visible publication/revision evidence when available, and independently reproducible integrity checks.

Treat the directory as a provenance-bearing dataset. A document may be useful only after the local bytes, metadata, classification, and coverage claim have all passed review.

## When to Use

Use this skill when you need to:

- add or refresh an insurer policy/terms document;
- find a direct official PDF for a company or insurance-type gap;
- classify or reclassify a document under the normalized taxonomy;
- audit `sources.json`, `index.csv`, the manifest, or the PDF hierarchy;
- decide whether a browser-reachable document is admissible to the local corpus;
- prepare a commit or PR containing policy documents and their provenance.

Do not use this workflow for brochures, comparison-site copies, customer application forms, claim-case material, answer keys, or documents that cannot be transferred and verified as local bytes.

## Canonical Artifacts

Keep these roles distinct:

- `collection-manifest.json`: approved collection inputs. Each entry has the company slug, proposed primary type, product name, official direct URL, and landing page.
- `sources.json`: authoritative provenance and attempt history. It may contain failed, duplicate, and superseded attempts in addition to verified rows. Do not collapse history merely to make counts look cleaner.
- `index.csv`: compact generated view of `sources.json`; regenerate it whenever provenance changes. It is not an independent source of truth.
- `<company>/<insurance-type>/*.pdf`: admitted local artifacts. Use stable ASCII paths and preserve Korean product names in metadata.
- `README.md` and `RESEARCH_SCOPE.md`: human-readable scope, taxonomy, current collection snapshot, limitations, and refresh procedure.
- `collect_policies.py`: reproducible downloader and verifier. Run it from the repository root.

A repeated path in `sources.json` is not automatically corruption: a failed historical attempt and a later verified attempt can legitimately reference the same intended output path. Audit by status and hash, not by demanding one row per path.

## Admission Workflow

### 1. Bind the request to scope

Start with `RESEARCH_SCOPE.md` and the current manifest. Identify the company, product family, and normalized primary type needed. Record a gap as unresolved rather than silently broadening the phase-1 company list.

Completion criterion: the proposed document has a named company slug, primary insurance type, product name, direct URL, and landing page, or the gap is explicitly recorded as unresolved.

### 2. Prefer official sources

Use the insurer's own product-disclosure page and direct file endpoint. Association directories and government pages may ground company discovery or standard-term research, but they are not substitutes for the insurer's product document when an insurer source is available.

Do not infer a download URL solely from a product name. Record the exact URL that returned the bytes and the landing page used to establish context. Query parameters, encoded Korean filenames, and insurer file endpoints are acceptable when they are official and reproducible.

Completion criterion: the URL resolves to an insurer-controlled host or an explicitly documented official endpoint, and its product context is recorded.

### 3. Retrieve without confusing reachability with admission

Run the collector first:

```text
/usr/bin/python docs/policy/collect_policies.py
```

If an insurer host rejects Python TLS but permits an official `curl` transfer, use the fallback only for retrieval, then apply the same PDF gates. A browser showing a valid PDF is evidence of source availability, not evidence that the corpus contains it. If the binary cannot be exported into the workspace, retain the finding as unresolved and do not create a placeholder or mirror copy.

Completion criterion: the exact bytes exist under the company/type hierarchy, or the attempt is recorded as failed without an invented artifact.

### 4. Apply the PDF integrity gates

Every admitted artifact must satisfy all of these checks:

1. HTTP status is 200.
2. Local bytes begin with `%PDF-`.
3. `pdfinfo` exits successfully and supplies a page count.
4. `pdftotext` can produce a short first-pages probe when the PDF permits extraction.
5. `bytes` and SHA-256 are recorded from the exact local file.
6. The provenance row has `status: verified-pdf` (or the explicitly justified `pdf-signature-only` fallback).

A file utility identifying a file as a PDF is insufficient. In particular, a partial transfer can have a PDF header while failing `pdfinfo`; reject it and remove the incomplete artifact.

Completion criterion: the local file, provenance row, and independent command output agree on signature, size, hash, and page-count status.

### 5. Classify from the document, not the URL

Choose one normalized primary type based on the product title and central coverage described in the terms. Do not classify from a stale filename, search-result label, or adjacent product page. Use `critical-illness` only when the document substantively covers serious diseases such as cancer, cerebrovascular disease, and/or ischemic heart disease; use `group` only when the terms are for group insurance.

If a product spans multiple risks, select the type that best represents the contract as a whole and explain a non-obvious reclassification in `RESEARCH_SCOPE.md` or the relevant README. Do not use a semantically wrong type merely to fill a coverage matrix.

Completion criterion: the folder name, manifest type, source row type, and documentation agree, and a reviewer can identify supporting wording in the PDF text probe or product title.

### 6. Preserve revision and attempt history

Never overwrite an older revision. A changed document receives a new revision-specific path and provenance entry. Identical content may be recorded as a duplicate with a pointer to the original. Retain failed attempts when they explain why a source was not admitted; do not turn an HTTP error, non-PDF response, browser-only result, or expired encrypted URL into a verified row.

Currentness is established by visible publication/revision or application dates in the document or official product context. Retrieval date alone does not make a document current.

Completion criterion: older files remain available, duplicate/failed statuses are intelligible, and no row claims currentness unsupported by source evidence.

### 7. Synchronize generated metadata

After any manifest or provenance change, regenerate `index.csv` from `sources.json`. Then verify that the row count and key fields (`company_slug`, `insurance_type`, `path`, `status`, `sha256`, and `source_url`) match in order. Do not hand-edit only the CSV.

Completion criterion: `sources.json` and `index.csv` agree, and the README/scope snapshot reflects the resulting verified corpus without hiding unresolved gaps.

## Independent Audit

Run a fresh audit after collection and before commit. The audit must cover both structural and semantic correctness.

### Structural audit

For every row whose status is `verified-pdf` or `pdf-signature-only`:

- the path exists;
- the bytes begin with `%PDF-`;
- the actual byte count equals `bytes`;
- the actual SHA-256 equals `sha256`;
- `pdfinfo` status and recorded page count are consistent;
- the corresponding CSV row agrees with the JSON row;
- the source URL is present and official.

Also check that failed and duplicate rows are not accidentally counted as verified, and that the manifest, provenance, index, and filesystem counts are explainable rather than merely equal by accident.

### Semantic and coverage audit

Review the product title, first-page text, publication/application date, and primary type for a sample of every newly admitted company/type. Specifically audit:

- currentness claims;
- thin-company additions;
- any type introduced to close a gap;
- reclassified group or critical-illness products;
- documents from browser-only or fallback transport paths;
- unresolved companies and type-level gaps.

A coverage matrix is a search aid, not proof of semantic coverage. Report a gap when no current official terms can be admitted. Never fill a gap with a brochure, stale document, unrelated rider, or fabricated retrieval result.

### Repository audit

Before staging:

- inspect `git status` and `git diff --stat`;
- stage only the policy corpus and relevant metadata/tooling;
- exclude unrelated screenshots, temporary files, credentials, and runtime artifacts;
- run the repository's relevant tests and `git diff --check`;
- review the staged file list, including binary count and total scope.

For a PR, verify the pushed head SHA, base branch, PR state, reviews, and status checks. An open, mergeable PR with no reviews or checks is not the same as a reviewed and CI-cleared change.

## Refresh and Closeout Checklist

- [ ] Scope and direct official URLs are recorded in `collection-manifest.json`.
- [ ] Every admitted PDF is locally transferable and passes signature plus `pdfinfo` gates.
- [ ] `sources.json` records status, URL, landing page, retrieval date, bytes, hash, pages, and text probe.
- [ ] `index.csv` was regenerated from `sources.json` and key fields match.
- [ ] Primary types are supported by product/terms content, not URL guesses.
- [ ] Older revisions, duplicates, failures, and browser-only findings remain intelligible.
- [ ] Currentness is supported by publication/revision/application evidence.
- [ ] README and scope documentation state verified coverage and residual gaps.
- [ ] No raw case sources, customer data, answer keys, credentials, or unrelated artifacts were added.
- [ ] Tests, integrity audit, and `git diff --check` pass.
- [ ] The staged file list and PR head SHA have been verified.

## Common Pitfalls

1. **Browser success treated as local collection success.** Requiring the binary to exist locally prevents unverifiable corpus claims.
2. **Partial PDF accepted because `file` says PDF.** `pdfinfo` is the decisive structural gate.
3. **A current-looking search result used as a policy source.** Follow the insurer landing page and retain the direct URL that supplied the bytes.
4. **Stale terms labeled current from retrieval date.** Use visible publication/revision/application dates or say currentness is unestablished.
5. **A gap filled with a semantically adjacent product.** Keep the gap open unless the document itself supports the normalized type.
6. **History deleted to simplify counts.** Preserve failed and duplicate attempts; count verified rows by status.
7. **`index.csv` edited independently.** Regenerate it from `sources.json` so provenance remains authoritative.
8. **Unrelated workspace artifacts staged with a large corpus.** Review the staged name list explicitly before committing.
9. **PR creation mistaken for review completion.** Check actual review decisions and status checks before merge or branch cleanup.
