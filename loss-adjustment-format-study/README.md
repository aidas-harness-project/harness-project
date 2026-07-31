# Loss-adjustment format study

Corpus research derived from a protected local PDF corpus under `../sources/`. The version-controlled package contains aggregate analysis, authoring contracts, reusable tools, and synthetic tests. Raw sources, extracted report PDF/text, OCR state, per-document metadata, source-path-bearing review artifacts, and the report index remain local and are ignored by Git. Full corpus validation and rebuilding therefore require the protected local fixture plus the sibling `tools/` directory. Run every command below from the repository root.

## Results

`analysis/corpus-metrics.json` is the canonical machine-readable source for these counts; the list below is its completed-study summary.

- 95 source PDFs inventoried
- 2,883 source pages OCR/text inspected
- 94 loss-adjustment report sections found
- 1 document classified as no report
- 864 report pages extracted
- 94 source-page PDF cuts and 94 OCR text files produced locally; these answer-key derivatives are not version-controlled
- All boundaries passed both primary and independent OCR/text review; all 95 independent decisions matched, and five representative forms also passed visual boundary sampling
- The validator checks live source-inventory closure, review-artifact hashes, OCR-cache/source binding, exact extracted text, and rendered equality of every selected source/output page

## Scope and assurance terms

- The source inventory includes every file whose `.pdf` suffix matches case-insensitively, recursively under `sources/`. Stable `DOC_###` identifiers follow the normalized relative-path sort order.
- A report section is one contiguous, 1-based inclusive range of physical source-PDF pages. This study supports at most one report range per source. The range includes report covers, submission material, substantive report pages, terminal evidence lists or signatures, and a closing divider when it separates the report from attachments. Actual attachments are excluded.
- Primary review is a human semantic judgment over every proposed classification and range. Each decision must be marked `verified` and include a non-empty rationale.
- Independent review is a separate judgment over every manifest entry and form-feed-delimited OCR sidecar. In this study it did not use the source PDFs or an independently produced OCR stream. Its privacy-minimized artifact contains document IDs, source-relative paths for source binding, classifications, ranges, and confidence, but no OCR text or raw source bytes. All 94 report ranges and the one no-report decision matched the primary review.
- Exact text means byte-for-byte equality with the selected pages reconstructed from the source-bound OCR sidecar, including the generated source-page markers. It does not mean agreement with a fresh independent OCR run.
- Rendered equality means equality of deterministic RGB render hashes for every selected source page and its extracted output page. It proves extraction fidelity, not semantic boundary correctness. The five visual samples separately inspect boundary meaning and span one form from each study family.
- Immutable means an enforced tool behavior and repository convention: the utility never opens source PDFs for writing. It is not a claim that the filesystem itself is mounted read-only.
- Review artifacts must declare non-empty, distinct primary and independent reviewer IDs. Those declarations and nested hashes establish internal consistency and reject same-ID review pairs, but they do not authenticate a person or independently prove organizational separation. Tamper evidence still requires an external trusted anchor such as a version-control commit or signed artifact digest.
- Repository-wide test counts in `analysis/validation-summary.json` are an observed working-tree snapshot, not a durable property of this study.

## Layout

Version-controlled artifacts:

- `analysis/corpus-metrics.json`: aggregate family/page/phrase metrics
- `analysis/boundary-sample-qa.md`: visual first/last-page checks across five report forms
- `analysis/validation-summary.json`: corpus, schema, focused-test, and repository-test status
- `analysis/format-analysis.md`: source-grounded format, hierarchy, tone, and reasoning study
- `analysis/llm-authoring-rules.md`: operational LLM drafting rules
- `analysis/loss-adjustment-report.schema.json`: structured authoring schema
- `analysis/examples/example-disease-benefit.json`: schema-valid pseudonymous example
- `../tools/generate_loss_adjustment_corpus_metrics.py`: deterministic metric definitions, generation, and staleness check

Protected local artifacts ignored by Git:

- `manifest.json`: source inventory, hashes, reviewed page ranges, output paths
- `.study.lock` and `.publication-journal.json`: transient exclusive-lock and crash-recovery state
- `boundary-review.json`: one reviewed boundary decision per source document
- `independent-boundary-review.json`: privacy-minimized all-document independent boundary clearance with source-relative binding paths
- `_ocr_cache/`: full-document OCR sidecars plus source-bound provenance metadata used for boundary review
- `sections/DOC_###/loss-adjustment-report.pdf`: original source pages copied directly
- `sections/DOC_###/loss-adjustment-report.txt`: OCR text with source-page markers
- `sections/DOC_###/metadata.json`: source/output hashes and exact range
- `no-report/DOC_###/metadata.json`: reviewed no-report outcome and source provenance
- `analysis/report-index.csv`: compact source/report index

## Prerequisites and reproducibility limits

The repository currently has no pinned root Python environment or study-specific lockfile. The extraction utility requires Linux with `fcntl`, process groups, procfs (`/proc/self/fd`), parent-death signaling, and `renameat2` exchange/no-replace support; other POSIX systems and Windows are not supported. The study tools require Python 3.10 or newer and PyMuPDF (`fitz`); report-schema validation also requires `jsonschema`. Rebuilding documents without a sufficient embedded text layer additionally requires the `ocrmypdf` command and Tesseract Korean and English language data. Test execution requires `pytest`.

The utility first uses embedded PDF text when every page has a sufficient text layer; otherwise it runs OCRmyPDF with Tesseract `kor+eng`. OCR runs in a dedicated process group with a 30-minute timeout and whole-group termination. Each OCRmyPDF process inherits only its study's lock descriptor, so abrupt parent termination does not permit another mutation of that study to delete or reuse its staging tree while that OCR process remains alive. Because these tool versions are not pinned, a fresh scan or full OCR refresh is environment-sensitive and is not promised to reproduce historical OCR hashes byte for byte. The v2 extraction contract binds cache validity to the exact extractor source bytes plus normalized PyMuPDF, OCRmyPDF, and Tesseract versions, so an implementation or toolchain change requires regeneration rather than relabeling old sidecars as current. Once a protected fixture has current reviewer and review-generation identities, extraction-contract-bound cache metadata, and the current report-index columns, validation is deterministic against those exact source and artifact bytes.

The protected local artifacts listed above are deliberately absent from a normal clone and from pull-request diffs. Their aggregate outputs are retained, but reproducing or checking corpus metrics requires authorized access to the complete local fixture. `analysis/validation-summary.json` explicitly separates historical protected-study facts from current-candidate evidence; it does not claim a corpus or metric-staleness pass when the fixture is absent. The synthetic test suite and example contract validation do not require that fixture.

## Validate the completed study

With the protected local fixture present:

```text
python tools/extract_loss_adjustment_sections.py validate loss-adjustment-format-study
python tools/generate_loss_adjustment_corpus_metrics.py loss-adjustment-format-study --check
python tools/validate_loss_adjustment_report.py \
  loss-adjustment-format-study/analysis/examples/example-disease-benefit.json
```

Success means the corpus validator reports `"valid": true` with an empty `errors` array, the metric checker reports that metrics are current, and the example validator reports `"valid": true` with an empty `errors` array.

From a normal clone without the protected fixture, run the focused synthetic tests and example validator:

```text
python -m pytest -q \
  tests/test_extract_loss_adjustment_sections.py \
  tests/test_generate_loss_adjustment_corpus_metrics.py \
  tests/test_validate_loss_adjustment_report.py \
  tests/test_loss_adjustment_schema_artifacts.py
python tools/validate_loss_adjustment_report.py \
  loss-adjustment-format-study/analysis/examples/example-disease-benefit.json
```

## Rebuild workflow

A rebuild is not fully automated. Inside this repository the study root is fixed to `loss-adjustment-format-study`; that is the only in-repository location whose generated artifacts are covered by the repository ignore rules. An arbitrary study root is accepted only outside the repository and requires its own access and exclusion controls. `scan` stages a complete OCR-cache candidate and replaces the cache and canonical manifest only after every worker has completed and the live source inventory still matches the captured generation; it does not create or approve either human review artifact. Mutating commands hold a no-follow lock opened relative to a retained study-directory descriptor, and each mutation continues through that descriptor if the pathname is replaced. Multi-root publication fsyncs the complete candidate, durably records prior and candidate fingerprints in `.publication-journal.json`, validates the exact operation-specific staging inventory, and uses atomic `renameat2` exchange/no-replace transitions with post-transition fingerprint checks. A `prepared` journal rolls back to the complete prior generation; after the durable `committed` phase, recovery completes the new generation and its verified cleanup; an interrupted rollback resumes from `rolled_back`. Unknown staging entries or a foreign target/backup generation block recovery and remain preserved for operator review. The journal is removed only after the selected all-old or all-new generation and exact transaction-owned cleanup are durable. Use a fresh external study directory unless replacing an existing external study is intentional. Do not force-add protected artifacts to Git.

```text
python tools/extract_loss_adjustment_sections.py scan sources loss-adjustment-format-study
# Author and independently verify loss-adjustment-format-study/boundary-review.json and
# loss-adjustment-format-study/independent-boundary-review.json as described below.
python tools/extract_loss_adjustment_sections.py apply-review loss-adjustment-format-study
python tools/extract_loss_adjustment_sections.py extract loss-adjustment-format-study
python tools/extract_loss_adjustment_sections.py validate loss-adjustment-format-study
python tools/generate_loss_adjustment_corpus_metrics.py loss-adjustment-format-study
python tools/generate_loss_adjustment_corpus_metrics.py loss-adjustment-format-study --check
```

For primary review, inspect each manifest proposal and its form-feed-delimited OCR pages. Create exactly one `boundary-review.json` decision for every manifest document and declare a non-empty `primary_reviewer_id` plus a non-empty `review_generation_id`. Every decision must bind the reviewed source and OCR generation with `document_id`, `source_relative_path`, `source_sha256`, `source_page_count`, and `ocr_sidecar_sha256`. A report decision also needs `contains_report: true`, valid `page_start` and `page_end`, `primary_review: "verified"`, and a non-empty `rationale`. A no-report decision uses `contains_report: false` and null page bounds. The completed `boundary-review.json` is the concrete shape reference.

The independent reviewer must separately classify all documents and record one source-and-OCR-generation-bound decision per document in `independent-boundary-review.json`, including `ocr_sidecar_sha256`, with a non-empty `reviewer_id`, a non-empty `review_generation_id`, status, decision count, mismatch count, confidence counts, and per-document source identity/classification/range/confidence. Its reviewer and generation IDs must each differ from the primary artifact's corresponding ID. There is no command that performs this judgment, authenticates an identity, or creates a trusted verified artifact. After reconciliation, `boundary-review.json` must bind the independent artifact by path, SHA-256, counts, and zero mismatches. `apply-review` applies the primary decisions atomically. `extract` revalidates both exact bound review files, one-to-one unique manifest coverage, current source and OCR identity, verified statuses, distinct declared reviewer and generation IDs, and zero independent mismatches before reading a cache or writing an output; final `validate` repeats those checks. If `refresh-cache` changes any sidecar digest, it revokes the applied review generation and resets every manifest record to pending review.

Metric generation also requires `loss-adjustment-format-study/analysis/report-index.csv`, with exactly one family assignment for every manifest document. The generator validates every index row against the exact manifest source path, SHA-256, byte length, classification, range, review status, and section-directory metadata, uses its reviewed `family` field as the sole family grouping source, and embeds the index SHA-256 in generated metrics. `scan` does not classify families or create this index; the completed index is the concrete shape and allowed-family reference. Review those assignments before generating metrics.

The required sequence is scan, human primary and independent review, apply-review, extract, validate, metrics generation, then metrics staleness check. Changes to sources, review decisions, OCR sidecars, extracted outputs, detector definitions, or indexed family assignments can invalidate downstream hashes or metrics and must be followed by validation and regeneration as appropriate.

For an older study whose OCR sidecars predate provenance metadata, run `python tools/extract_loss_adjustment_sections.py refresh-cache loss-adjustment-format-study` before extraction. This re-extracts or OCRs every document from a descriptor-captured, hash-verified source snapshot; it does not merely bless legacy cache text. The refreshed cache is built privately and published with the manifest as one journaled generation, so a late worker, exception, or process termination recovers a coherent complete previous or committed new cache-and-manifest generation.

The absence of the corresponding `_ocr_cache/DOC_###.json` provenance record, a record that does not match its source and sidecar hashes, or a record whose `extraction_contract_sha256` does not match the current embedded-text/OCR configuration identifies a legacy or invalid cache entry. Prefer `refresh-cache` when a full source-derived refresh is feasible.

Reviewed legacy text is not sufficient to claim the current extraction contract. `adopt-reviewed-cache` therefore rejects sidecars that lack matching current-contract metadata and directs the operator to `refresh-cache`; it cannot mint a current OCR configuration digest for historical text. The 12 recomputed and 83 reviewed-legacy counts retained in `analysis/validation-summary.json` are explicitly historical and do not clear current-candidate corpus validation.

## Authoring contract

The corpus feeds `format-analysis.md`; those observed patterns are translated into operational drafting requirements in `llm-authoring-rules.md`, machine-enforced fields and cross-field invariants in `loss-adjustment-report.schema.json` plus the custom validator, and a pseudonymous conformance fixture in `analysis/examples/example-disease-benefit.json`. Corpus tendencies are not automatically mandatory unless the rules or validators make them so.

The intended producer is an LLM-assisted drafting system and the intended output is a non-final structured report for professional review. Evidence, calculation, medical, and legal gate values are model-authored preflight states; `passed` is not authenticated proof that a human or professional review occurred, and open professional-judgment content prevents the medical/legal gates from self-clearing. `draft` means the document remains non-final; `review_required` means one or more gates require human resolution. Neither status is approval. Human release approval belongs to a separate trusted workflow and is deliberately not represented by this authoring schema.

The study utility rejects repository `data/`, `outputs/`, `source-cases/`, and `archive/sources/` as source/study roots, rejects symlink components, walks the source tree through no-follow directory descriptors, retains authorized PDF bytes through OCR/extraction/validation, and rechecks the complete live inventory immediately before publication. It confines all manifest paths, requires the OCR cache and report/no-report output roots to close exactly over the manifest projection, and publishes staged scan, cache-refresh, reviewed-cache-adoption, and extraction generations under a descriptor-anchored exclusive lock with object-bound CAS, a durable phase journal, and startup recovery. It never modifies source PDFs. The authoring validator also rejects embedded approval records and model-authored final approval claims.
