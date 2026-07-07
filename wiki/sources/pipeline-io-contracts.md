---
type: Source
title: 컴포넌트별 I/O 계약 초안 (GPT 정리)
description: Phase 1 18개·Phase 2 13개 컴포넌트의 입출력 JSON 예시, 공통 필드, 파일 매핑, 우선 확정할 핵심 I/O 5개를 정의한 계약 초안.
resource: ../../sources/pipeline_input-output.md
tags: [source, pipeline, io-contract, schema]
timestamp: 2026-07-07T00:00:00+09:00
---

# 핵심 내용

[개편된 파이프라인](../pipeline.md)의 각 컴포넌트에 대해 "무엇을 받아
어떤 JSON을 만들고, 다음 단계는 그중 어떤 필드를 참조하는가"를 정의한
**컴포넌트 I/O 계약 v0.1**.

- **공통 output 필드**: `case_id`, `run_id`, `component`, `status`,
  `created_at`, `model_info`(모델명·프롬프트 버전), `confidence`,
  `review_required`, `reviewer_role`, `evidence_references`, `warnings`.
  판단성 출력에는 `source_grounded`, `hallucination_risk_check`,
  `prohibited_language_check` 추가 → [표준 출력 계약](../templates/component-output.md)에 반영됨.
- **Phase 1** (18개): Case Intake부터 Evaluation까지 컴포넌트별
  Input/Output JSON 예시 + "다음 단계로 넘기는 핵심 필드" 명시.
- **Phase 2** (13개): 보험사 응답 intake부터 초안 v2 업데이트까지.
  OCR/Redaction은 Phase 1 schema 재사용, 파일명만 분리.
- **파일 경로 규약**: `data/raw/CASE_XXX/`(원본) →
  `data/processed/CASE_XXX/DOC_XXX/`(중간) → `outputs/CASE_XXX/`(산출물).
- **JSON Schema 작업 순서**: 공통 계약 → Week 1 → Week 2 → Week 3 순으로
  `schemas/*.schema.json` 확정.
- **최우선 핵심 I/O 5개 (스크리닝 backbone)**:
  1. `document_manifest.json` — 문서 목록표 (목차이자 출석부)
  2. `classification_result.json` — 문서 이름표
  3. `extracted_claim_fields.json` — 핵심 사실 카드
  4. `denial_reason_result.json` — 보험사 주장 요약표 (R코드 분류)
  5. `screening_report.json` — 1차 브리핑 자료 (md 리포트의 원천)

# 평가 (2026-07-06)

**채택 적절.** 특히 이전 ingest에서 결정한 조정 사항이 모두 반영되어
있다 — 사건 유형 분류 포함(#12, `template_id` 선택까지), 벡터 인덱싱
optional 명시, Evidence Validation의 불일치 명시적 출력
(`inconsistencies` 배열 + severity), 감액사유 추출의 Week 2 당김 실행.
좋은 설계 포인트: 컴포넌트마다 "다음 단계로 넘기는 핵심 필드"를 따로
정의해 결합도를 낮춘 것, 필드 단위 confidence·근거·검수 라우팅
(손사/의사 구분), Critic의 문장 단위 지적 + `suggested_revision`,
`expert_review.json`/`evaluation_result.json`이 [평가 지표](../evaluation/metrics.md)
루브릭과 정합적인 것.

발견된 보완점 (반영 완료 또는 구현 시 주의):

> ⚠️ 갭 1: screening_report 예시(JSON·md 모두)에 위키
> [스크리닝 리포트 템플릿](../templates/screening-report.md)의
> **§7 "1차 판단"(진행 가능성·난이도·우선 검토 포인트)이 없다.**
> JSON에 `preliminary_assessment` 블록을 추가해야 한다.

> ⚠️ 갭 2: `evaluation_result.json`은 케이스 단위다. PoC 성공 기준은
> **전체 케이스 집계**(정확도 80% 등)이므로 케이스별 결과를 합산하는
> aggregate 평가 리포트가 별도로 필요하다.

갭 1·2의 해결 계획(주차 배치 포함)은
[파이프라인 이해 가이드](../answers/pipeline-understanding-and-gap-plan.md)에 정리됨.

구현 시 주의: ① `document_manifest.json`은 Intake가 만들고 OCR·분류가
계속 갱신하는 공유 상태다 — 단계별로 자기 필드만 추가하는 규율이 없으면
덮어쓰기 사고가 난다. ② `extracted_claim_fields`의 필드 구조가 필드마다
다르다(단일 value vs 기간형 start/end/days) — JSON Schema 확정 시 필드
타입을 고정할 것. ③ 문서 유형·담보에 영어 표준 코드가 도입됐다
(`diagnosis_certificate`, `injury_disability` 등) — taxonomy 페이지에
코드 열로 반영함.

# Citations

[1] [I/O 계약 초안 원문](../../sources/pipeline_input-output.md)
[2] [파이프라인 구조](../pipeline.md)
