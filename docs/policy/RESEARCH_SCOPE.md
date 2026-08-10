# Korean Insurance Policy Corpus: Research Scope

Status: phase-1 list approved; initial official-document collection completed on 2026-08-03.

## Objective

Collect publicly available, current Korean insurance policy/terms documents (`보험약관`, including applicable special clauses where available) from official insurer websites and organize them under this directory by company and normalized insurance type.

"All possible insurance types" is not a finite, stable list at the product level: insurers publish many product-specific riders, revisions, and sales channels. The practical completion target is therefore:

1. cover the major life and non-life insurer families;
2. cover the common insurance-type taxonomy below at least once, and preferably across multiple companies;
3. preserve the insurer's product name, publication/revision date, official source URL, and retrieval date;
4. never label a document as current when the source does not establish that fact.

We will collect policy terms, not brochures, product summaries, advertisements, or comparison-site copies. Official insurer domains are the preferred source. Association/government pages may be used for company discovery and public standard terms, but each downloaded policy should retain its original source URL.

## Proposed phase-1 company set

### Life insurers

| Company | Korean name | Why included / expected type coverage |
| --- | --- | --- |
| Kyobo Life | 교보생명 | Large general life insurer; protection, health, savings, annuity |
| Samsung Life | 삼성생명 | Large general life insurer; protection, health, savings, annuity, variable |
| Hanwha Life | 한화생명 | Large general life insurer; protection, health, savings, annuity |
| NH NongHyup Life | NH농협생명 | Broad protection and annuity catalogue; agricultural/rural distribution |
| Shinhan Life | 신한라이프 | Broad protection, health, retirement/annuity catalogue |
| KB Life | KB라이프생명 | Broad protection, health, retirement/annuity catalogue |
| Mirae Asset Life | 미래에셋생명 | Savings, variable, retirement/annuity and protection |
| DB Life | DB생명 | Protection and health products |
| Dongyang Life | 동양생명 | Protection, health, child and annuity products |
| Lina Life | 라이나생명 | Health, cancer and dental specialization |

### Non-life insurers

| Company | Korean name | Why included / expected type coverage |
| --- | --- | --- |
| Samsung Fire & Marine | 삼성화재 | Broad personal/commercial non-life; auto, health, property, liability |
| DB Insurance | DB손해보험 | Broad personal/commercial non-life; auto, health, property, liability |
| Hyundai Marine & Fire | 현대해상 | Broad personal/commercial non-life; auto, health, child, property |
| KB Insurance | KB손해보험 | Broad personal/commercial non-life; auto, health, property, liability |
| Meritz Fire & Marine | 메리츠화재 | Health, accident, long-term and property products |
| Hanwha General Insurance | 한화손해보험 | Auto, health, accident and property products |
| NH NongHyup General Insurance | NH농협손해보험 | Auto, agricultural, property, health and liability products |
| AXA General Insurance | AXA손해보험 | Auto and direct non-life products |
| AIG Korea | AIG손해보험 | Travel, accident, property and commercial/specialty products |
| Carrot General Insurance | 캐롯손해보험 | Direct/usage-based auto and short-term digital products |

## Phase-2 extension, if phase 1 leaves type or market gaps

These companies are listed for follow-up rather than silently expanding the first pass:

- Life: 흥국생명, iM라이프, AIA생명, KDB생명, 교보라이프플래닛
- Non-life: 롯데손해보험, 흥국화재, 하나손해보험, 신한EZ손해보험, 카카오페이손해보험, 서울보증보험, 처브손해보험/에이스손해보험
- Reinsurance/specialty: 코리안리재보험, where a publicly downloadable direct policy document is actually applicable to the corpus

## Normalized insurance-type coverage matrix

The same document may be indexed under one primary type and several applicable tags. Product-specific riders remain separate documents when the insurer publishes them separately.

### Life / personal protection

- term life (`정기보험`)
- whole/permanent life (`종신보험`)
- endowment/savings (`저축성보험`)
- variable insurance (`변액보험`)
- health/disease (`건강보험`)
- cancer (`암보험`)
- critical illness / serious disease (`중대질병보험`)
- accident (`재해보험`)
- disability / income protection (`장해·소득보장보험`)
- child / juvenile (`어린이보험`)
- dental (`치아보험`)
- dementia / long-term care (`치매·간병보험`)
- annuity / retirement (`연금보험`)
- group insurance (`단체보험`), only where public terms are available

### Non-life / general insurance

- indemnity medical (`실손의료보험`)
- health / accident (`질병·상해보험`)
- auto (`자동차보험`)
- driver (`운전자보험`)
- fire / home / property (`화재·주택·재산보험`)
- liability (`배상책임보험`)
- travel (`여행자보험`)
- pet (`반려동물보험`)
- marine / cargo (`해상·적하보험`), when publicly available
- cyber (`사이버보험`), when publicly available
- agriculture / livestock (`농작물·가축보험`), where public terms are available
- title / guarantee (`권원·보증보험`), mainly specialty/non-life

## Source and file rules

- Use official insurer domains for downloads whenever possible.
- Record the exact landing page and direct download URL; do not rely on a URL inferred from a product name.
- Verify downloaded bytes as a PDF and record HTTP status, content type, byte size, SHA-256, retrieval timestamp, and any visible publication/revision date.
- Use stable ASCII filenames for scripts and references; preserve Korean product/company names in metadata and index files.
- Do not overwrite an older revision. A changed document gets a new revision-specific file and metadata entry.
- Do not download or store personal information, application forms containing customer data, or claim-case answer keys.
- Do not treat a document as a legal conclusion or as advice; this is a source corpus for clause retrieval and review.

## Discovery sources

The phase-1 company list is grounded in the member directories of the two Korean insurance associations:

- Life Insurance Association of Korea member directory: https://www.klia.or.kr/klia/company/member/list.do
- General Insurance Association of Korea member directory: https://www.knia.or.kr/m/about/partner/partner01

The association directories are discovery references only. The corpus metadata must point to the insurer's own policy/terms publication page for each downloaded file.

## Collection result and layout

The refreshed collection contains 120 structurally verified PDFs from 19 of the 20 phase-1 companies. It now includes broad current life and non-life coverage, including variable, pet, cyber/financial-fraud, child/health, critical illness, group, annuity, travel, liability, and fire/property terms. Mirae Asset Life is the only completely unrepresented phase-1 company: its official browser session returned a valid current PDF, but the binary could not be transferred into this workspace and the local HTTP client timed out. Remaining type-level gaps are disability/income protection as a primary type, marine/cargo, agriculture/livestock, and title/guarantee insurance. Group insurance is covered by `캐롯단체상해보험`, normalized under `group`; critical illness is represented by Hanwha Life’s H건강플러스 terms, which define cancer, cerebrovascular disease, and ischemic heart disease coverage. AXA’s newly stored current policy is driver insurance, not automobile insurance; AXA auto terms remain unadmitted.

The collection is organized as:

```text
docs/policy/
  README.md
  index.csv
  sources.json
  collection-manifest.json
  collect_policies.py
  <company-slug>/<insurance-type-slug>/
    <document>.pdf
```

`index.csv` is the compact tabular index. `sources.json` is the authoritative provenance record, including HTTP status, content type, byte count, SHA-256, PDF page count, and a short text probe. The two files are generated from the same collection pass and should be refreshed together.
