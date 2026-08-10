# Korean Insurance Policy Documents

This directory contains the first official-source collection pass for the approved Korean insurer scope.

Collection snapshot: 2026-08-03

## What is here

- 120 verified PDF policy/terms documents
- 19 of the 20 phase-1 companies represented
- 727,643,819 bytes of downloaded PDFs
- 119 manifest entries and 120 verified PDF artifacts
- Company/type hierarchy below this directory
- `index.csv`: compact index for spreadsheet and shell use
- `sources.json`: authoritative provenance and verification records
- `collection-manifest.json`: approved input manifest used by the collector
- `collect_policies.py`: reproducible downloader and verifier
- `RESEARCH_SCOPE.md`: company list, taxonomy, source rules, and known gaps

The PDFs are Korean insurer terms documents (`보험약관`) rather than brochures or comparison-site copies. Each accepted file was fetched from an insurer-controlled domain, returned HTTP 200, began with `%PDF-`, and passed `pdfinfo`. SHA-256 hashes, byte sizes, page counts, direct URLs, landing pages, and a short text probe are recorded in `sources.json` and `index.csv`.

## Represented companies

| Company | Slug | Verified documents |
| --- | --- | ---: |
| AIG Korea | `aig-korea` | 2 |
| AXA Insurance | `axa-fire` | 2 |
| Carrot General Insurance | `carrot-fire` | 9 |
| DB Insurance | `db-insurance` | 10 |
| DB Life | `db-life` | 2 |
| Dongyang Life Insurance | `dongyang-life` | 4 |
| Hanwha General Insurance | `hanwha-general` | 10 |
| Hanwha Life Insurance | `hanwha-life` | 12 |
| Hyundai Marine & Fire Insurance | `hyundai-marine` | 13 |
| KB Insurance | `kb-insurance` | 15 |
| KB Life Insurance | `kb-life` | 4 |
| Kyobo Life Insurance | `kyobo-life` | 2 |
| Lina Life Insurance | `lina-life` | 2 |
| Meritz Fire & Marine Insurance | `meritz-fire` | 1 |
| NH NongHyup General Insurance | `nh-fire` | 2 |
| NH NongHyup Life Insurance | `nh-life` | 4 |
| Samsung Fire & Marine Insurance | `samsung-fire` | 11 |
| Samsung Life Insurance | `samsung-life` | 9 |
| Shinhan Life Insurance | `shinhan-life` | 6 |

Mirae Asset Life is the only phase-1 company still needing a stored first-pass document. Its official product-disclosure page and current terms endpoint were reached in an isolated browser session and returned a valid current PDF, but the browser environment could not transfer the binary into this workspace and the local HTTP client timed out against the host. No mirror was substituted.

## Type coverage in this pass

Covered at least once: term, whole/permanent life, life rider, variable, health, cancer, critical illness, accident, child/juvenile, dental, dementia/long-term care, annuity, savings, indemnity medical, auto, driver, fire/property, liability, travel, pet, group, cyber/financial-fraud, and one unspecified life disclosure document.

Not yet covered in the downloaded files: disability/income protection as a primary type, marine/cargo, agriculture/livestock, and title/guarantee insurance. Group insurance is covered by the reclassified `캐롯단체상해보험` artifact, and critical illness is represented by Hanwha Life’s H건강플러스 terms covering cancer, cerebrovascular disease, and ischemic heart disease. The taxonomy and intended follow-up sources are in `RESEARCH_SCOPE.md`.

## Transport notes

- AIG Korea and DB Life were fetched with `curl` after the insurer hosts closed Python TLS connections. Both responses were independently validated as PDFs.
- DB Life’s live product catalog is `https://www.idblife.com/notice/product/sale`; the current whole-life terms were validated through the insurer’s file endpoint using the curl fallback.
- Lina’s current `무배당 라이나브레인케어건강보험` terms were fetched from its official disclosure PDF endpoint and validated as a 51-page PDF.
- Meritz Fire & Marine requires one browser/session-backed JSON lookup before its encrypted file-download URL works. The current driver terms were retrieved in one persistent session and validated as a 46-page PDF; the encrypted URL is retained in provenance and may expire for future refreshes.
- Meritz’s current driver and health terms were also independently validated in the official browser session, but the browser sandbox cannot export those larger replacement binaries into the workspace.
- DB Insurance cyber was located through the official product catalog/API and fetched from the insurer’s exact `피싱·해킹 금융사기보상보험` file endpoint.
- The expanded pass added stable official terms for current pet, cyber, variable, child/health, fire/property, liability, travel, and savings products across the participating insurers.
- Mirae Asset Life remains unresolved only at the workspace binary-transfer step; its official browser response was confirmed as a valid PDF.

## Refresh procedure

1. Review `collection-manifest.json` and update direct URLs from the insurers' current product-disclosure pages.
2. Run from the repository root:

   ```text
   /usr/bin/python docs/policy/collect_policies.py
   ```

3. The collector streams files directly into the company/type hierarchy, verifies the PDF signature and `pdfinfo`, calculates SHA-256, and regenerates `sources.json` and `index.csv`.
4. Existing PDFs are not overwritten. A re-fetched file gets a date-suffixed filename; identical content is retained as a duplicate provenance entry.
5. Inspect HTTP failures and the `status` field before treating a refresh as complete.

The collector does not modify `source-cases/`, `archive/sources/`, `outputs/`, `data/`, or any ledger/run-state file. It also does not download customer forms or claim-case material.

## Source quality and legal caution

The source URLs in the metadata are the insurer's direct files or insurer-controlled file endpoints. Association directories were used only to ground company discovery:

- https://www.klia.or.kr/klia/company/member/list.do
- https://www.knia.or.kr/m/about/partner/partner01

These documents are a research corpus, not legal advice. Product availability, wording, and revisions can change. Always re-check the insurer's current product-disclosure page before using a document operationally.
