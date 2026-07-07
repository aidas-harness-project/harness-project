---
type: Answer
title: 파이프라인 이해 가이드 + 미해결 갭 해결 계획
description: 파이프라인을 3개 관점(Phase·묶음 Agent·backbone I/O)으로 읽는 법과, 소스 분석에서 나온 갭 2건의 해결 계획.
question: 파이프라인을 어떻게 이해하면 되나? 소스 분석에서 발견된 모순 지점들은 어떻게 해결할 계획인가?
tags: [answer, pipeline, io-contract, evaluation]
timestamp: 2026-07-07T00:00:00+09:00
---

# 파이프라인을 읽는 세 가지 해상도

같은 파이프라인을 보는 세 겹의 관점이다. 상세는
[파이프라인 구조](../pipeline.md) 참고.

1. **업무 흐름 관점 — 2개 Phase**: Phase 1(최초 청구/검토 → 스크리닝
   리포트 + 초안 v1), Phase 2(반려·감액 대응 → 반박 포인트 + 초안 v2).
   실제 손사 업무 순서(청구 → 보험사 응답 → 대응)를 따른다. PoC에서는
   종결 케이스라 보험사 안내문이 처음부터 있으므로 두 Phase를 같은
   입력에 순차 실행하고, 감액사유 추출만 Week 2로 당긴다.
2. **구현 관점 — 7개 묶음 Agent**: 설계는 31개 컴포넌트(18+13)로
   세분화하되 코드는 7개 묶음으로 시작. **컴포넌트 = 설계·계약 단위,
   묶음 Agent = 코드 단위.**
3. **데이터 관점 — backbone I/O 5개**: 컴포넌트끼리는 JSON 파일로만
   통신(`data/raw/` → `data/processed/` → `outputs/CASE_XXX/`).
   스크리닝까지의 최소 경로인 backbone 5개(`document_manifest` →
   `classification_result` → `extracted_claim_fields` →
   `denial_reason_result` → `screening_report`)가 스키마 확정
   우선순위다. 이 5개가 확정되면 나머지는 병렬로 붙일 수 있다.

# 모순·갭 현황 (2026-07-07 기준)

| # | 내용 | 상태 |
| --- | --- | --- |
| 모순 1 | 파이프라인 초안에 사건 유형 분류 누락 | ✅ 해소 — Phase 1 #12 추가, I/O 계약에 `template_id`까지 반영 |
| 모순 2 | 감액사유 추출이 Week 3으로 밀림 | ✅ 해소 — Week 2로 당김, I/O 계약에 반영 |
| 갭 1 | `screening_report.json`에 §7 "1차 판단" 없음 | ⛔ 미해결 → 아래 계획 |
| 갭 2 | 평가가 케이스 단위뿐, 성공 기준은 전체 집계 | ⛔ 미해결 → 아래 계획 |

모순 1·2의 발견·해소 경위는 [파이프라인 초안 평가](../sources/pipeline-rough-gpt.md),
갭 1·2의 발견 경위는 [I/O 계약 평가](../sources/pipeline-io-contracts.md) 참고.

# 갭 해결 계획

## 갭 1 — `preliminary_assessment` 블록 추가 (Week 2)

`schemas/screening_report.schema.json` 확정 시
[스크리닝 리포트 템플릿](../templates/screening-report.md) §7을 채우는
블록을 추가한다:

```json
"preliminary_assessment": {
  "feasibility": "high | medium | low",
  "difficulty": "high | medium | low",
  "priority_review_points": ["..."],
  "rationale_evidence_references": ["..."]
}
```

주의: 이 블록은 파이프라인에서 가장 판단성이 강한 출력이다(사실 추출이
아니라 "진행 가능성" 의견). [표준 출력 계약](../templates/component-output.md)의
`source_grounded` 체크를 반드시 걸고, `review_required: true` +
`reviewer_role: "손해사정사"`를 기본값으로 둔다. backbone 5번 파일이므로
Week 2 스키마 확정 목록에 포함하면 별도 일정 부담이 없다.

## 갭 2 — aggregate 평가 리포트 신설 (Week 3)

케이스 단위 `evaluation_result.json` 위에 집계 단계를 얹는다:
[Evaluation Harness](../agents/evaluation-harness.md)가 마지막에
`outputs/*/evaluation_result.json`을 전부 읽어
`outputs/evaluation_summary.json`을 생성.

- 내용: [평가 지표](../evaluation/metrics.md)의 정량 목표별 케이스
  합산치 + 목표 달성 여부, [Go/No-Go](../evaluation/go-no-go.md) 항목별
  판정 근거.
- LLM 없이 순수 집계 스크립트로 충분 — 구현 비용 반나절 이하, Go/No-Go
  회의 자료가 자동 생성되는 효과.

## 함께 처리할 구현 주의 2건 (Week 1)

갭은 아니지만 스키마 확정 때 같이 처리한다
([I/O 계약 평가](../sources/pipeline-io-contracts.md)의 주의사항):

- `document_manifest.json`은 Intake가 만들고 OCR·분류가 이어 갱신하는
  공유 파일 → 스키마에 **필드별 owner 컴포넌트**를 명시하고 "자기
  필드만 추가, 기존 필드 수정 금지" 규칙을 계약에 넣는다.
- `extracted_claim_fields`는 필드마다 구조가 다름(단일 값 vs 기간형
  start/end/days) → JSON Schema에서 필드 타입을 고정한다.

# Citations

[1] [파이프라인 구조](../pipeline.md)
[2] [I/O 계약 초안 평가](../sources/pipeline-io-contracts.md)
[3] [파이프라인 초안 평가](../sources/pipeline-rough-gpt.md)
