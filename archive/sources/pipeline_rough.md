아래처럼 정리하면 됩니다. 핵심은 **Phase 1은 “초기 검토/초안 생성”**, **Phase 2는 “보험사 반려·감액 이후 대응/초안 업데이트”**로 분리하는 것입니다. PoC 기준으로는 3주 안에 OCR, 문서분류, 핵심항목 추출, 청구담보·감액사유 추출, 약관 후보 매핑, 반박 포인트, 손사서 초안 v1, 평가까지 검증하는 구조입니다. 

---

# Phase 1. 최초 청구/검토 파이프라인

## 목표

초기 케이스 원자료를 입력하면 AI Agent Harness가 문서를 구조화하고, 핵심 항목을 추출하고, 약관 후보를 매핑한 뒤, **스크리닝 리포트와 손해사정서 초안 v1**을 생성한다.

## 전체 흐름

```text
Case Intake
↓
OCR / Text Extraction
↓
Redaction / 가명처리
↓
Document Classification
↓
Document Preprocessing
↓
Embedding / Vector Indexing
↓
Policy Document Processing
↓
Policy Clause Extraction
↓
Policy Clause Normalization
↓
Claim Field Extraction
↓
Coverage Identification
↓
Requirement Matching
↓
Evidence Validation
↓
Screening Report Generation
↓
Draft Report Generation v1
↓
Evidence Check / Critic Agent
↓
Human Review
↓
Evaluation Harness
```

---

## Phase 1 컴포넌트별 정리

| 순서 | 컴포넌트                          | 목적                     | Input                           | Output                                              | 담당 Agent / Layer              |
| -: | ----------------------------- | ---------------------- | ------------------------------- | --------------------------------------------------- | ----------------------------- |
|  1 | Case Intake                   | 케이스 단위 작업 생성           | 케이스 폴더, 원본 PDF/이미지, 문서 목록       | `case_manifest.json`, `document_manifest.json`      | `CaseIntakeAgent`             |
|  2 | OCR / Text Extraction         | PDF·이미지에서 텍스트 추출       | 보험증권, 약관, 진단서, 의무기록, 영수증, 안내문 등 | `ocr_result.json`, 페이지별 `text.md`                   | `OCRAgent`                    |
|  3 | Redaction / 가명처리              | 개인정보 및 민감정보 제거         | OCR text                        | `redacted_text.md`, `redaction_log.json`            | `RedactionAgent`              |
|  4 | Document Classification       | 문서 유형 분류               | 가명처리 텍스트, 문서 메타데이터              | `classification_result.json`                        | `DocumentClassifierAgent`     |
|  5 | Document Preprocessing        | 검색·추출에 적합하게 문서 정리      | 분류된 문서 텍스트                      | `cleaned_text.md`, `page_chunks.json`               | `PreprocessAgent`             |
|  6 | Embedding / Vector Indexing   | RAG 검색 기반 구축           | 문서 chunk, metadata              | vector index, `chunk_metadata.json`                 | `IndexingAgent`               |
|  7 | Policy Document Processing    | 약관 문서를 조항 단위로 구조화      | 약관 OCR text, 약관 PDF             | `policy_clause.json`                                | `PolicyProcessorAgent`        |
|  8 | Policy Clause Extraction      | 지급요건·면책·감액 관련 조항 추출    | 약관 chunk                        | `policy_clause.json`                                | `PolicyClauseExtractionAgent` |
|  9 | Policy Clause Normalization   | 약관 조항을 표준 필드로 정규화      | 약관 조항 원문                        | `normalized_policy_clause.json`                     | `PolicyNormalizerAgent`       |
| 10 | Claim Field Extraction        | 청구 검토에 필요한 핵심 필드 추출    | 진단서, 의무기록, 영수증, 보험청구서           | `extracted_claim_fields.json`                       | `FieldExtractionAgent`        |
| 11 | Coverage Identification       | 청구담보 식별                | 보험증권, 약관, 청구서                   | `coverage_result.json`                              | `CoverageAgent`               |
| 12 | Requirement Matching          | 담보별 지급요건과 청구 자료 매칭     | 정규화 약관, 추출 필드, 청구담보             | `requirement_matching_result.json`                  | `RequirementMatchingAgent`    |
| 13 | Evidence Validation           | 근거 확인 및 문서 간 불일치 탐지    | 전체 문서, 추출값, 약관 매칭 결과            | `evidence_validation_result.json`                   | `EvidenceValidationAgent`     |
| 14 | Screening Report Generation   | 1차 검토 리포트 생성           | 구조화 JSON 전체                     | `screening_report.json`, `screening_report.md`      | `ScreeningReportAgent`        |
| 15 | Draft Report Generation v1    | 손해사정서 초안 v1 생성         | 스크리닝 리포트, 약관 후보, 근거 문서          | `draft_report_v1.md`, `draft_report_metadata.json`  | `DraftWriterAgent`            |
| 16 | Evidence Check / Critic Agent | 환각, 금지 표현, 근거 없는 판단 탐지 | 손사서 초안 v1                       | `critic_result.json`, `draft_report_v1_reviewed.md` | `CriticAgent`                 |
| 17 | Human Review                  | 전문가 검수                 | 초안, critic 결과, 근거 자료            | `expert_review.json`, 수정 의견                         | 손해사정사 / 의사 / 법률전문가            |
| 18 | Evaluation Harness            | 실제 최종 손사서와 비교 평가       | 모델 output, 정답지, 전문가 평가          | `evaluation_result.json`, `evaluation_report.md`    | `EvaluationAgent`             |

---

## Phase 1 주요 input/output 예시

### Input

```json
{
  "case_id": "CASE_001",
  "source_folder": "data/raw/CASE_001/",
  "documents": [
    {
      "document_id": "DOC_001",
      "file_path": "data/raw/CASE_001/diagnosis_certificate.pdf",
      "expected_document_type": null
    },
    {
      "document_id": "DOC_002",
      "file_path": "data/raw/CASE_001/insurance_policy.pdf",
      "expected_document_type": null
    }
  ]
}
```

### 최종 Output

```json
{
  "case_id": "CASE_001",
  "phase": "phase1_initial_review",
  "outputs": {
    "case_manifest": "outputs/CASE_001/case_manifest.json",
    "document_manifest": "outputs/CASE_001/document_manifest.json",
    "extracted_claim_fields": "outputs/CASE_001/extracted_claim_fields.json",
    "coverage_result": "outputs/CASE_001/coverage_result.json",
    "requirement_matching_result": "outputs/CASE_001/requirement_matching_result.json",
    "evidence_validation_result": "outputs/CASE_001/evidence_validation_result.json",
    "screening_report": "outputs/CASE_001/screening_report.md",
    "draft_report_v1": "outputs/CASE_001/draft_report_v1.md",
    "critic_result": "outputs/CASE_001/critic_result.json"
  },
  "review_required": true,
  "reviewer_role": "손해사정사"
}
```

---

# Phase 2. 보험사 반려/감액 이후 대응 파이프라인

## 목표

보험사의 반려·감액·부지급 문서가 들어오면 AI Agent Harness가 사유를 구조화하고, 기존 약관·근거 자료와 비교하여 **반박 포인트를 생성**한 뒤, 기존 손해사정서 초안을 **v2로 업데이트**한다.

## 전체 흐름

```text
Insurer Response Intake
↓
OCR / Text Extraction
↓
Redaction / 가명처리
↓
Denial / Reduction Reason Extraction
↓
Denial Reason Taxonomy Classification
↓
Policy-to-Denial Matching
↓
Existing Evidence Retrieval
↓
Evidence Validation Against Denial
↓
Rebuttal Point Generation
↓
Draft Report Update
↓
Evidence Check / Critic Agent
↓
Human Review
↓
Evaluation Harness
```

---

## Phase 2 컴포넌트별 정리

| 순서 | 컴포넌트                                  | 목적                   | Input                               | Output                                              | 담당 Agent / Layer           |
| -: | ------------------------------------- | -------------------- | ----------------------------------- | --------------------------------------------------- | -------------------------- |
|  1 | Insurer Response Intake               | 보험사 반려·감액·부지급 문서 접수  | 보험사 안내문, 지급거절 통지서, 감액 안내문           | `insurer_response_result.json`                      | `InsurerResponseAgent`     |
|  2 | OCR / Text Extraction                 | 보험사 문서 텍스트화          | 보험사 PDF/이미지                         | `ocr_result.json`, `insurer_response_text.md`       | `OCRAgent`                 |
|  3 | Redaction / 가명처리                      | 개인정보 제거              | 보험사 문서 OCR text                     | `redacted_insurer_response.md`                      | `RedactionAgent`           |
|  4 | Denial / Reduction Reason Extraction  | 반려·감액·부지급 사유 원문 추출   | 보험사 안내문 text                        | `denial_reason_result.json`                         | `DenialReasonAgent`        |
|  5 | Denial Reason Taxonomy Classification | 사유를 표준 taxonomy로 분류  | 추출된 사유 후보                           | `denial_reason_result.json`                         | `DenialTaxonomyAgent`      |
|  6 | Policy-to-Denial Matching             | 반려사유와 관련 약관 조항 연결    | 반려사유, 약관 index, normalized clause   | `policy_to_denial_matching_result.json`             | `PolicyDenialMatcherAgent` |
|  7 | Existing Evidence Retrieval           | 기존 케이스 자료에서 관련 근거 검색 | 반려사유, case vector index             | `retrieved_evidence.json`                           | `EvidenceRetrievalAgent`   |
|  8 | Evidence Validation Against Denial    | 보험사 주장과 기존 자료 비교     | 반려사유, 약관 조항, 의무기록, 진단서 등            | `evidence_validation_result.json`                   | `EvidenceValidationAgent`  |
|  9 | Rebuttal Point Generation             | 반박 포인트 후보 생성         | 반려사유, 약관, 근거자료, 검증 결과               | `rebuttal_points.json`, `rebuttal_points.md`        | `RebuttalAgent`            |
| 10 | Draft Report Update                   | 기존 초안 업데이트           | `draft_report_v1.md`, 반박 포인트, 검증 결과 | `draft_report_v2.md`, `draft_report_metadata.json`  | `DraftUpdateAgent`         |
| 11 | Evidence Check / Critic Agent         | 업데이트된 초안 검증          | `draft_report_v2.md`                | `critic_result.json`, `draft_report_v2_reviewed.md` | `CriticAgent`              |
| 12 | Human Review                          | 전문가 최종 검수            | 초안 v2, critic 결과, 근거                | `expert_review.json`                                | 손해사정사 / 의사 / 법률전문가         |
| 13 | Evaluation Harness                    | 반박 포인트와 업데이트 품질 평가   | output, 실제 최종 손사서, 전문가 평가           | `evaluation_result.json`                            | `EvaluationAgent`          |

---

## Phase 2 주요 input/output 예시

### Input

```json
{
  "case_id": "CASE_001",
  "phase": "phase2_denial_response",
  "existing_outputs": {
    "case_manifest": "outputs/CASE_001/case_manifest.json",
    "normalized_policy_clause": "outputs/CASE_001/normalized_policy_clause.json",
    "extracted_claim_fields": "outputs/CASE_001/extracted_claim_fields.json",
    "evidence_validation_result": "outputs/CASE_001/evidence_validation_result.json",
    "draft_report_v1": "outputs/CASE_001/draft_report_v1.md"
  },
  "new_documents": [
    {
      "document_id": "DOC_020",
      "file_path": "data/raw/CASE_001/insurer_reduction_notice.pdf",
      "document_type": "insurer_response"
    }
  ]
}
```

### 최종 Output

```json
{
  "case_id": "CASE_001",
  "phase": "phase2_denial_response",
  "outputs": {
    "insurer_response_result": "outputs/CASE_001/insurer_response_result.json",
    "denial_reason_result": "outputs/CASE_001/denial_reason_result.json",
    "policy_to_denial_matching_result": "outputs/CASE_001/policy_to_denial_matching_result.json",
    "rebuttal_points": "outputs/CASE_001/rebuttal_points.json",
    "draft_report_v2": "outputs/CASE_001/draft_report_v2.md",
    "critic_result": "outputs/CASE_001/critic_result_v2.json"
  },
  "review_required": true,
  "reviewer_role": "손해사정사"
}
```

---

# Phase 1과 Phase 2 비교 요약

| 구분        | Phase 1                      | Phase 2                          |
| --------- | ---------------------------- | -------------------------------- |
| 핵심 목적     | 초기 케이스 검토 및 손사서 초안 v1 생성     | 보험사 반려·감액 이후 반박 포인트 및 초안 v2 생성   |
| 주요 입력     | 원자료 전체                       | 보험사 반려/감액/부지급 문서 + 기존 Phase 1 결과 |
| 핵심 문서     | 진단서, 의무기록, 보험증권, 약관, 영수증     | 보험사 안내문, 감액 통지서, 부지급 사유서         |
| 핵심 output | 스크리닝 리포트, 약관 후보, 손사서 초안 v1   | 반려사유 분석, 반박 포인트, 손사서 초안 v2       |
| 주요 Agent  | 문서분류, 필드추출, 약관처리, 담보식별, 초안작성 | 반려사유추출, taxonomy분류, 반박생성, 초안업데이트 |
| 사람 검수     | 약관 해석, 의료 쟁점, 초안 문장          | 반려사유 해석, 반박 논리, 의료·법률 표현         |
| 자동화 적합도   | 높음: OCR, 분류, 추출, 리포트 초안      | 중간~높음: 사유 추출, 근거 검색, 반박 후보 생성    |
| 위험        | OCR 오류, 필드 오추출, 약관 후보 부정확    | 보험사 사유 오해, 과도한 반박, 법률·의료 단정      |

---

# 컴포넌트별 표준 Output 필드

각 컴포넌트의 output은 공통적으로 아래 필드를 포함하는 것을 권장합니다.

```json
{
  "case_id": "CASE_001",
  "component": "FieldExtractionAgent",
  "run_id": "RUN_20260706_001",
  "status": "success",
  "confidence": 0.82,
  "evidence_references": [
    {
      "document_id": "DOC_001",
      "page": 1,
      "quote": "진단명: 요추 추간판탈출증"
    }
  ],
  "review_required": true,
  "reviewer_role": "손해사정사",
  "source_grounded": true,
  "hallucination_risk_check": {
    "risk_level": "low",
    "reason": "원문 근거 문장과 직접 연결됨"
  },
  "prohibited_language_check": {
    "passed": true,
    "issues": []
  }
}
```

---

# 3주 PoC 기준 최소 구현 파이프라인

3주 안에 모든 컴포넌트를 완벽히 나누기보다는 아래처럼 축약하는 것을 추천합니다.

## Week 1: OCR + 분류 + 핵심항목 추출

```text
Case Intake
↓
OCR
↓
Redaction
↓
Document Classification
↓
Claim Field Extraction
```

필수 산출물:

```text
case_manifest.json
document_manifest.json
ocr_result.json
classification_result.json
extracted_claim_fields.json
```

## Week 2: 스크리닝 + 약관 매핑

```text
Policy Processing
↓
Coverage Identification
↓
Requirement Matching
↓
Evidence Validation
↓
Screening Report Generation
```

필수 산출물:

```text
policy_clause.json
normalized_policy_clause.json
coverage_result.json
requirement_matching_result.json
evidence_validation_result.json
screening_report.md
```

## Week 3: 반박 포인트 + 손사서 초안 + 평가

```text
Denial Reason Extraction
↓
Rebuttal Point Generation
↓
Draft Report Generation
↓
Critic Agent
↓
Human Review
↓
Evaluation Harness
```

필수 산출물:

```text
denial_reason_result.json
rebuttal_points.json
draft_report_v1.md
critic_result.json
evaluation_result.json
```

---

# 실무적으로 추천하는 컴포넌트 묶음

3주 PoC에서는 너무 많은 agent로 쪼개면 오히려 관리가 어려우므로 다음처럼 묶는 것이 좋습니다.

| PoC용 묶음 Agent             | 내부 기능                              | 이유                             |
| ------------------------- | ---------------------------------- | ------------------------------ |
| `DocumentPipelineAgent`   | OCR, 가명처리, 문서분류, 전처리               | 문서 처리 단계는 하나의 pipeline으로 묶기 좋음 |
| `PolicyPipelineAgent`     | 약관 처리, 조항 추출, 정규화                  | 약관 관련 작업은 서로 강하게 연결됨           |
| `ClaimAnalysisAgent`      | 핵심항목 추출, 담보 식별, 지급요건 매칭            | 초기 스크리닝의 핵심                    |
| `EvidenceValidationAgent` | 근거 검증, 불일치 탐지, hallucination check | 안전장치의 핵심                       |
| `DenialResponseAgent`     | 반려사유 추출, taxonomy 분류, 약관 매칭        | Phase 2 핵심                     |
| `ReportGenerationAgent`   | 스크리닝 리포트, 손사서 초안 생성, 업데이트          | 문서 산출물 담당                      |
| `CriticEvaluationAgent`   | 금지 표현 탐지, 평가, 실패 유형 기록             | harness 품질 관리                  |

즉, 설계 문서상으로는 컴포넌트를 세분화하되, 실제 PoC 구현에서는 **7개 정도의 agent 묶음**으로 시작하는 것이 적절합니다.
