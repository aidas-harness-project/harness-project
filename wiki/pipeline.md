---
type: Architecture
title: Agent Harness 파이프라인
description: Phase 1(최초 청구/검토)과 Phase 2(반려·감액 대응) 파이프라인의 컴포넌트별 input/output 파일, 구현용 7개 묶음 Agent, 3주 일정 매핑.
tags: [pipeline, agent-harness]
timestamp: 2026-07-07T00:00:00+09:00
---

파이프라인은 두 Phase로 나뉜다 — 실제 손해사정 업무 흐름(청구 → 보험사
응답 → 대응)을 따른 구조다. 초기 설계였던 단일 14단계 흐름을
[GPT 파이프라인 초안](sources/pipeline-rough-gpt.md) ingest 후 개편했다
(조정 내역은 해당 소스 페이지의 평가 참고).

파일 경로 규약: 원본 `data/raw/CASE_XXX/` → 중간 산출물
`data/processed/CASE_XXX/DOC_XXX/` → 최종 산출물 `outputs/CASE_XXX/`.
**굵은 파일**은 `schemas/`에 JSON Schema v0.1이 확정된 것, 나머지는
Week 1→2→3 순으로 확정 예정 ([I/O 계약](sources/pipeline-io-contracts.md)).

# Phase 1 — 최초 청구/검토

케이스 원자료 입력 → 문서 구조화 → 약관 매핑 → **스크리닝 리포트 +
손사서 초안 v1**.

| 순서  | 컴포넌트                                                  | Input                                                         | Output                                                                                          | 관련 개념                                                                    |
| --- | ----------------------------------------------------- | ------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| 1   | Case Intake                                           | `data/raw/CASE_XXX/` 폴더                                       | `case_manifest.json`, **`document_manifest.json`** (파일명·형식·크기)                                  | [Document Intake](agents/document-intake.md)                             |
| 2   | OCR / Text Extraction                                 | manifest + 원본 문서                                              | `ocr_result.json`, `page_*.md` + manifest 갱신 (`pages`·`ocr_status`·`ocr_text_path`)             | [OCR Layer](agents/ocr-layer.md)                                         |
| 3   | Redaction                                             | `page_*.md`                                                   | `redacted_text.md`, `redaction_result.json` + manifest 갱신 (`redacted_text_path`)                | [Redaction](agents/redaction.md)                                         |
| 4   | Document Classification                               | `redacted_text.md`                                            | **`classification_result.json`** + manifest 갱신 (`document_type`·`classification_confidence`)    | [Document Classification](agents/document-classification.md)             |
| 5   | Document Preprocessing                                | 분류된 문서 텍스트                                                    | `page_chunks.json`                                                                              | (신규 — 문서 처리 묶음에 포함)                                                      |
| 6   | (optional) Vector Indexing                            | `page_chunks.json`                                            | `index_metadata.json`                                                                           | 케이스 수 적은 PoC에서는 보류                                                       |
| 7~9 | Policy Processing → Clause Extraction → Normalization | 약관 문서 텍스트 (classification으로 선별)                               | `policy_clause.json` → `policy_clause_extraction_result.json` → `normalized_policy_clause.json` | [Policy Mapping](agents/policy-mapping.md)의 전처리 세분화                      |
| 10  | Claim Field Extraction                                | 진단서·의무기록 `redacted_text.md` (classification으로 선별)             | **`extracted_claim_fields.json`**                                                               | [Field Extraction](agents/field-extraction.md)                           |
| 11  | Coverage Identification                               | 보험증권·약관 텍스트 + claim fields                                    | `coverage_result.json`                                                                          | [Claim Coverage](agents/claim-coverage.md)                               |
| 12  | **Case Type Classification**                          | `extracted_claim_fields` + `coverage_result`                  | `case_type_result.json` (`template_id` 포함)                                                      | [Case Type](agents/case-type.md) — 초안에 누락돼 있어 추가 (P0, 템플릿 선택 기준)         |
| 13  | Requirement Matching                                  | `coverage_result` + `normalized_policy_clause` + claim fields | `requirement_matching_result.json`                                                              | [Policy Mapping](agents/policy-mapping.md) 확장                            |
| 14  | Evidence Validation                                   | claim fields + matching + `page_chunks`                       | `evidence_validation_result.json` (**문서 간 불일치** `inconsistencies` 배열 포함)                        | [Consistency Check](agents/consistency-check.md)                         |
| 15  | Screening Report Generation                           | 위 구조화 산출물 전체 + **`denial_reason_result.json`** (Week 2 당김)    | **`screening_report.json`** (§7 `preliminary_assessment` 필수), `screening_report.md`             | [템플릿](templates/screening-report.md)                                     |
| 16  | Draft Report Generation v1                            | `screening_report` + `case_type_result`의 `template_id`        | `draft_report_metadata.json`, `draft_report_v1.md`                                              | [Draft Writer](agents/draft-writer.md), [템플릿](templates/draft-report.md) |
| 17  | Critic Agent                                          | `draft_report_v1.md`                                          | `critic_result.json`, `draft_report_v1_reviewed.md`                                             | [Critic](agents/critic.md)                                               |
| 18  | Human Review → Evaluation                             | 산출물 전체 + 정답지 (`POC/` 최종 손사서 — 모델 입력 금지, 평가 전용)                | `expert_review.json`, `evaluation_result.json`                                                  | [Evaluation Harness](agents/evaluation-harness.md)                       |

# Phase 2 — 보험사 반려/감액 이후 대응

보험사 반려·감액·부지급 문서 입력 + Phase 1 산출물 → **반박 포인트 +
손사서 초안 v2**.

| 순서 | 컴포넌트 | Input | Output | 관련 개념 |
| --- | --- | --- | --- | --- |
| 1~3 | Insurer Response Intake → OCR → Redaction | 보험사 반려·감액 문서 | `insurer_response_result.json` + OCR·가명처리 산출물 (Phase 1 schema 재사용, 파일명만 분리) | Phase 1과 동일 계층 재사용 |
| 4~5 | Denial Reason Extraction → Taxonomy Classification | 가명처리된 안내문 텍스트 | `denial_reason_extraction_result.json` → **`denial_reason_result.json`** ([R코드](taxonomy/reduction-reasons.md) + Top-3용 `candidate_codes` + 감액금액) | [Denial Reason](agents/denial-reason.md) |
| 6 | Policy-to-Denial Matching | `denial_reason_result` + `normalized_policy_clause` | `policy_to_denial_matching_result.json` | [Policy Mapping](agents/policy-mapping.md) |
| 7~8 | Evidence Retrieval → Validation Against Denial | 감액사유 + 케이스 기존 자료 (`page_chunks`, claim fields) | `retrieved_evidence.json`, `denial_validation_result.json` | [Consistency Check](agents/consistency-check.md) |
| 9 | Rebuttal Point Generation | `denial_validation_result` | `rebuttal_points.json`, `rebuttal_points.md` | [Rebuttal](agents/rebuttal.md), [형식](templates/rebuttal-points.md) |
| 10 | Draft Report Update | `draft_report_v1.md` + `rebuttal_points` | `draft_report_update_result.json`, `draft_report_v2.md` | [Draft Writer](agents/draft-writer.md) |
| 11~13 | Critic → Human Review → Evaluation | `draft_report_v2.md` + 정답지 | `critic_result_v2.json`, `draft_report_v2_reviewed.md`, `evaluation_result.json` | [Critic](agents/critic.md), [Evaluation Harness](agents/evaluation-harness.md) |

**PoC 주의**: 종결 케이스는 보험사 안내문이 처음부터 케이스 팩에 있으므로
두 Phase를 같은 입력에 대해 순차 실행한다. 감액사유 추출은 아키텍처상
Phase 2 소속이지만, [스크리닝 리포트](templates/screening-report.md) §2
(보험사 판단)가 요구하므로 **일정상 Week 2에 당겨 실행**한다.

# 구현 묶음 — 7개 Agent

설계 문서상 컴포넌트는 위처럼 세분화하되, 3주 PoC 구현은 7개 묶음으로
시작한다. 묶음 경계에서 주고받는 파일이 곧 통합 지점이다.

| 묶음 Agent | 내부 컴포넌트 | 경계 Input | 경계 Output |
| --- | --- | --- | --- |
| `DocumentPipelineAgent` | OCR, 가명처리, 문서분류, 전처리 | **`document_manifest.json`** + 원본 문서 | `redacted_text.md`, **`classification_result.json`**, `page_chunks.json` (+ manifest 갱신 완료) |
| `PolicyPipelineAgent` | 약관 처리, 조항 추출, 정규화 | 약관 문서 텍스트 | `normalized_policy_clause.json` |
| `ClaimAnalysisAgent` | 핵심항목 추출, 담보 식별, **사건 유형 분류**, 지급요건 매칭 | `redacted_text.md` + **`classification_result.json`** + `normalized_policy_clause.json` | **`extracted_claim_fields.json`**, `coverage_result.json`, `case_type_result.json`, `requirement_matching_result.json` |
| `EvidenceValidationAgent` | 근거 검증, 불일치 탐지, hallucination check | claim fields + matching + `page_chunks.json` (+ 감액사유) | `evidence_validation_result.json`, `retrieved_evidence.json`, `denial_validation_result.json` |
| `DenialResponseAgent` | 반려사유 추출, taxonomy 분류, 약관 매칭 | 가명처리된 안내문 텍스트 + `normalized_policy_clause.json` | **`denial_reason_result.json`**, `policy_to_denial_matching_result.json` |
| `ReportGenerationAgent` | 스크리닝 리포트, 손사서 초안 생성·업데이트 | 구조화 산출물 전체 + `rebuttal_points.json` | **`screening_report.json`**/`.md`, `draft_report_v1.md`, `draft_report_v2.md` |
| `CriticEvaluationAgent` | 금지 표현 탐지, 평가, 실패 유형 기록 | 초안 + 정답지 (`POC/`, 평가 전용) | `critic_result.json`, `rebuttal_points.json`, `expert_review.json`, `evaluation_result.json` |

모든 컴포넌트 출력은 [컴포넌트 표준 출력 계약](templates/component-output.md)을
따른다.

# 컴포넌트 I/O 계약

각 컴포넌트의 입출력 JSON 형식과 "다음 단계로 넘기는 핵심 필드"는
[I/O 계약 초안](sources/pipeline-io-contracts.md)에 정의되어 있다.

가장 먼저 확정할 **스크리닝 backbone I/O 5개**:
`document_manifest.json` → `classification_result.json` →
`extracted_claim_fields.json` → `denial_reason_result.json` →
`screening_report.json`.

**JSON Schema 초안 v0.1이 `schemas/`에 있다** — 공통 계약
(`common_component_output.schema.json`) + backbone 5개. 원자료 예시로
검증 완료. [agents/](agents/index.md) 요구조건 대조 검증(2026-07-07)에서
4건 보완: ① manifest에 파일 형식·크기 필드 추가 (Document Intake 요구),
② `denial_reason_result`에 Top-3 평가용 `candidate_codes` 추가
([평가 지표](evaluation/metrics.md)의 Top-3 일치율 산정용),
③ screening 불일치 항목에 비교 대상 문서 추적(`related_documents`) 추가,
④ 부지급/감액 표현 후보 추출은 [Field Extraction](agents/field-extraction.md)이
아닌 `DenialResponseAgent`가 안내문에서 직접 수행하는 것으로 정리.

파이프라인을 처음 읽을 때의 관점 정리와 미해결 갭 2건의 해결 계획은
[파이프라인 이해 가이드](answers/pipeline-understanding-and-gap-plan.md) 참고.

# 3주 일정 매핑

| 주차 | 범위 | 필수 산출물 |
| --- | --- | --- |
| Week 1 | Case Intake → OCR → Redaction → 분류 → 핵심항목 추출 | manifest, ocr_result, classification_result, extracted_claim_fields |
| Week 2 | 약관 처리 → 담보 식별 → **사건 유형 분류** → 지급요건 매칭 → 근거 검증 → **감액사유 추출(당김)** → 스크리닝 리포트 | policy_clause(정규화 포함), coverage_result, requirement_matching_result, evidence_validation_result, denial_reason_result, screening_report.md |
| Week 3 | 반박 포인트 → 초안 v1/v2 → Critic → Human Review → 평가 | rebuttal_points, draft_report_v1/v2.md, critic_result, evaluation_result |

[PoC 가이드](references/poc-guide.md)의 Step 1~3과 동일한 골격이다.

# 우선순위

- **P0**: OCR/텍스트 추출, 문서 유형 분류, 핵심항목 추출, 청구담보 추출,
  부지급·감액사유 추출, 사건 유형 분류.
- **P1**: 문서 간 불일치 탐지, 약관 조항 후보 매핑(정규화·지급요건 매칭
  포함), 반박 포인트 생성, 손사서 초안 구조·v1/v2 작성.
- **Optional/보류**: 벡터 인덱싱 (직접 프롬프팅/BM25로 시작, 규모가
  커지면 도입).

# Citations

[1] [PoC 가이드](references/poc-guide.md)
[2] [Phase별 파이프라인 초안 (GPT 정리)](sources/pipeline-rough-gpt.md)
[3] [컴포넌트별 I/O 계약 초안 (GPT 정리)](sources/pipeline-io-contracts.md)
