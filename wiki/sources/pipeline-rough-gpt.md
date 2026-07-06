---
type: Source
title: Phase별 파이프라인 초안 (GPT 정리)
description: 파이프라인을 Phase 1(최초 청구/검토)과 Phase 2(반려·감액 대응)로 분리하고 구현용 7개 묶음 Agent를 제안한 설계 초안.
resource: ../../sources/pipeline_rough.md
tags: [source, pipeline, 설계]
timestamp: 2026-07-06T00:00:00+09:00
---

# 핵심 내용

- **Phase 분리**: Phase 1 = 초기 케이스 검토 → 스크리닝 리포트 + 손사서
  초안 v1. Phase 2 = 보험사 반려·감액·부지급 문서 접수 → 반박 포인트 +
  초안 v2 업데이트. 실제 손사 업무 흐름(청구 → 보험사 응답 → 대응)을 따름.
- **Phase 1**: 18개 컴포넌트 (intake → OCR → 가명처리 → 분류 → 전처리 →
  벡터 인덱싱 → 약관 처리/조항 추출/정규화 → 필드 추출 → 담보 식별 →
  지급요건 매칭 → 근거 검증 → 스크리닝 리포트 → 초안 v1 → Critic →
  Human Review → 평가).
- **Phase 2**: 13개 컴포넌트 (보험사 응답 intake → 사유 추출 → taxonomy
  분류 → 약관-반려사유 매칭 → 기존 근거 검색 → 검증 → 반박 생성 →
  초안 업데이트 → Critic → Human Review → 평가).
- **표준 output 필드**: 모든 컴포넌트가 `case_id`, `run_id`, `confidence`,
  `evidence_references`, `review_required`, `hallucination_risk_check`,
  `prohibited_language_check`를 공통 포함.
- **구현 묶음**: 설계상 세분화하되 실제 구현은 7개 묶음 Agent로 시작
  (DocumentPipeline / PolicyPipeline / ClaimAnalysis / EvidenceValidation /
  DenialResponse / ReportGeneration / CriticEvaluation).
- **주차 매핑**: Week 1 문서 처리·추출, Week 2 약관·스크리닝, Week 3
  반박·초안·평가.

# 평가 (2026-07-06)

전반적으로 **채택 적절**. [PoC 가이드](../references/poc-guide.md)의 14단계
파이프라인과 정합적이며 다음 4가지가 실질적 개선이다: ① Phase 1/2 분리가
실제 업무 흐름과 일치, ② 표준 output 필드가 가이드의 confidence·근거문장
저장 요구를 계약으로 일반화, ③ 설계 세분화 + 7개 묶음 구현의 이원화가
3주 제약에 현실적, ④ Human Review의 명시적 단계화.

다만 아래 조정이 필요했고, [파이프라인 구조](../pipeline.md)에 반영됨:

> ⚠️ 모순 1: 이 초안에는 **사건 유형 분류(Case Type Classification)가
> 없다.** [PoC 가이드](../references/poc-guide.md)에서는 P0 기능이며
> [손사서 템플릿](../templates/draft-report.md) 선택의 기준이다.
> → Phase 1의 ClaimAnalysis 묶음에 추가하는 것으로 반영.

> ⚠️ 모순 2: 이 초안은 **감액사유 추출을 Phase 2(Week 3)로 미뤘으나**,
> [스크리닝 리포트](../templates/screening-report.md) §2(보험사 판단)와
> PoC 가이드 Step 2는 감액사유 추출을 Week 2 산출물로 요구한다. 종결
> 케이스 PoC에서는 보험사 안내문이 처음부터 케이스 팩에 있으므로 미룰
> 이유도 없다. → 아키텍처상 Phase 2 소속은 유지하되, PoC 일정에서는
> Week 2에 당겨 실행하는 것으로 반영.

기타 조정: 문서 간 불일치 탐지는 Evidence Validation에 흡수되어 있는데
평가 지표에 별도 항목이 있으므로 명시적 출력으로 유지. 벡터 인덱싱은
케이스 수가 적은 PoC에서는 과투자일 수 있어 optional로 강등(직접 프롬프팅
/BM25로 시작, 필요 시 도입).

# Citations

[1] [파이프라인 초안 원문](../../sources/pipeline_rough.md)
[2] [PoC 가이드](../references/poc-guide.md)
