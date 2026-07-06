---
type: Architecture
title: Agent Harness 파이프라인
description: Phase 1(최초 청구/검토)과 Phase 2(반려·감액 대응)로 나뉜 파이프라인 구조와 구현용 7개 묶음 Agent, 3주 일정 매핑.
tags: [pipeline, agent-harness]
timestamp: 2026-07-06T00:00:00+09:00
---

파이프라인은 두 Phase로 나뉜다 — 실제 손해사정 업무 흐름(청구 → 보험사
응답 → 대응)을 따른 구조다. 초기 설계였던 단일 14단계 흐름을
[GPT 파이프라인 초안](sources/pipeline-rough-gpt.md) ingest 후 개편했다
(조정 내역은 해당 소스 페이지의 평가 참고).

# Phase 1 — 최초 청구/검토

케이스 원자료 입력 → 문서 구조화 → 약관 매핑 → **스크리닝 리포트 +
손사서 초안 v1**.

| 순서 | 컴포넌트 | 산출물 | 관련 개념 |
| --- | --- | --- | --- |
| 1 | Case Intake | case/document manifest | [Document Intake](agents/document-intake.md) |
| 2 | OCR / Text Extraction | 페이지별 텍스트, 품질 로그 | [OCR Layer](agents/ocr-layer.md) |
| 3 | Redaction | 가명처리 텍스트, redaction log | [Redaction](agents/redaction.md) |
| 4 | Document Classification | 문서 유형 + confidence | [Document Classification](agents/document-classification.md) |
| 5 | Document Preprocessing | cleaned text, page chunks | (신규 — 문서 처리 묶음에 포함) |
| 6 | (optional) Vector Indexing | 검색 인덱스 | 케이스 수 적은 PoC에서는 보류 |
| 7~9 | Policy Processing → Clause Extraction → Normalization | 정규화된 약관 조항 JSON | [Policy Mapping](agents/policy-mapping.md)의 전처리 세분화 |
| 10 | Claim Field Extraction | 핵심항목 JSON | [Field Extraction](agents/field-extraction.md) |
| 11 | Coverage Identification | 청구담보 + 근거 | [Claim Coverage](agents/claim-coverage.md) |
| 12 | **Case Type Classification** | 사건 유형 | [Case Type](agents/case-type.md) — 초안에 누락돼 있어 추가 (P0, 템플릿 선택 기준) |
| 13 | Requirement Matching | 담보별 지급요건-자료 매칭 | [Policy Mapping](agents/policy-mapping.md) 확장 |
| 14 | Evidence Validation | 근거 검증 + **문서 간 불일치**(명시적 출력 유지) | [Consistency Check](agents/consistency-check.md) |
| 15 | Screening Report Generation | 스크리닝 리포트 | [템플릿](templates/screening-report.md) |
| 16 | Draft Report Generation v1 | 손사서 초안 v1 | [Draft Writer](agents/draft-writer.md), [템플릿](templates/draft-report.md) |
| 17 | Critic Agent | 검수 태그, reviewed 초안 | [Critic](agents/critic.md) |
| 18 | Human Review → Evaluation | 전문가 검수, 평가 리포트 | [Evaluation Harness](agents/evaluation-harness.md) |

# Phase 2 — 보험사 반려/감액 이후 대응

보험사 반려·감액·부지급 문서 입력 + Phase 1 산출물 → **반박 포인트 +
손사서 초안 v2**.

| 순서 | 컴포넌트 | 산출물 | 관련 개념 |
| --- | --- | --- | --- |
| 1~3 | Insurer Response Intake → OCR → Redaction | 가명처리된 보험사 문서 | Phase 1과 동일 계층 재사용 |
| 4~5 | Denial Reason Extraction → Taxonomy Classification | 감액사유 + [R코드](taxonomy/reduction-reasons.md) + 감액금액 | [Denial Reason](agents/denial-reason.md) |
| 6 | Policy-to-Denial Matching | 반려사유-약관 조항 연결 | [Policy Mapping](agents/policy-mapping.md) |
| 7~8 | Evidence Retrieval → Validation Against Denial | 보험사 주장 vs 기존 자료 비교 | [Consistency Check](agents/consistency-check.md) |
| 9 | Rebuttal Point Generation | 반박 포인트 | [Rebuttal](agents/rebuttal.md), [형식](templates/rebuttal-points.md) |
| 10 | Draft Report Update | 손사서 초안 v2 | [Draft Writer](agents/draft-writer.md) |
| 11~13 | Critic → Human Review → Evaluation | 검수·평가 | [Critic](agents/critic.md), [Evaluation Harness](agents/evaluation-harness.md) |

**PoC 주의**: 종결 케이스는 보험사 안내문이 처음부터 케이스 팩에 있으므로
두 Phase를 같은 입력에 대해 순차 실행한다. 감액사유 추출은 아키텍처상
Phase 2 소속이지만, [스크리닝 리포트](templates/screening-report.md) §2
(보험사 판단)가 요구하므로 **일정상 Week 2에 당겨 실행**한다.

# 구현 묶음 — 7개 Agent

설계 문서상 컴포넌트는 위처럼 세분화하되, 3주 PoC 구현은 7개 묶음으로
시작한다.

| 묶음 Agent | 내부 컴포넌트 |
| --- | --- |
| `DocumentPipelineAgent` | OCR, 가명처리, 문서분류, 전처리 |
| `PolicyPipelineAgent` | 약관 처리, 조항 추출, 정규화 |
| `ClaimAnalysisAgent` | 핵심항목 추출, 담보 식별, **사건 유형 분류**, 지급요건 매칭 |
| `EvidenceValidationAgent` | 근거 검증, 불일치 탐지, hallucination check |
| `DenialResponseAgent` | 반려사유 추출, taxonomy 분류, 약관 매칭 |
| `ReportGenerationAgent` | 스크리닝 리포트, 손사서 초안 생성·업데이트 |
| `CriticEvaluationAgent` | 금지 표현 탐지, 평가, 실패 유형 기록 |

모든 컴포넌트 출력은 [컴포넌트 표준 출력 계약](templates/component-output.md)을
따른다.

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
