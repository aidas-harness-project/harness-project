---
type: Template
title: 컴포넌트 표준 출력 계약
description: 파이프라인의 모든 컴포넌트 출력이 공통으로 포함해야 하는 필드 — confidence, 근거 참조, 검수 플래그, 환각·금지표현 체크.
tags: [template, pipeline, contract]
timestamp: 2026-07-06T00:00:00+09:00
---

[파이프라인](../pipeline.md)의 모든 컴포넌트 출력 JSON은 아래 공통 필드를
포함한다. 근거 문장 저장·confidence 부여·검수 필요 표시라는
[PoC 가이드](../references/poc-guide.md)의 요구를 하나의 계약으로 일반화한
것이다 ([GPT 파이프라인 초안](../sources/pipeline-rough-gpt.md)에서 채택).

# Examples

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

# 필드 규칙

- `evidence_references` — 모든 주장·추출값은 원문(문서 ID + 페이지 + 인용)과
  연결한다. 연결 불가능하면 `source_grounded: false` + `review_required: true`.
- `reviewer_role` — 손해사정사 / 의사 / 법률전문가 중 검수 주체.
- `prohibited_language_check` — [금지 표현 가이드](forbidden-expressions.md)
  위반 여부. [Critic Agent](../agents/critic.md)가 최종 검증한다.

# Citations

[1] [Phase별 파이프라인 초안 (GPT 정리)](../sources/pipeline-rough-gpt.md)
