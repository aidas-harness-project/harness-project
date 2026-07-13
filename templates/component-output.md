---
type: Template
title: 컴포넌트 표준 출력 계약
description: 파이프라인의 모든 컴포넌트 출력이 공통으로 포함해야 하는 필드 — 실행 메타데이터, confidence, 근거 참조, 검수 플래그, 환각·금지표현 체크.
tags: [template, pipeline, contract]
timestamp: 2026-07-13T00:00:00+09:00
adopted_from: wiki/templates/component-output.md
---

> **provenance**: `wiki/templates/component-output.md`에서 2026-07-13에
> harness-project로 채택한 사본. 이 파일이 1차 소스 — [draft-report.md](draft-report.md)
> 참고. 실제 스키마는 `schemas/common_component_output.schema.json`이 권위
> 있는 정의이며, 이 문서는 그 배경 설명이다.

[pipeline.md](../pipeline.md)의 모든 컴포넌트 출력 JSON은 아래 공통 필드를
포함한다. 근거 문장 저장·confidence 부여·검수 필요 표시라는 요구를 하나의
계약으로 일반화한 것.

# 공통 필드 (모든 output)

```json
{
  "case_id": "CASE_001",
  "run_id": "RUN_20260706_001",
  "component": "FieldExtractionAgent",
  "status": "success",
  "created_at": "2026-07-06T15:30:00+09:00",
  "model_info": {
    "model_name": "claude-sonnet-5",
    "prompt_version": "field_extraction_v0.1"
  },
  "confidence": 0.82,
  "review_required": true,
  "reviewer_role": "손해사정사",
  "evidence_references": [],
  "warnings": []
}
```

# 안전 필드 (판단성 output에 추가)

```json
{
  "evidence_references": [
    {
      "document_id": "DOC_001",
      "page": 1,
      "quote": "진단명: 요추 추간판탈출증"
    }
  ],
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
  판단 성격에 따라 필드 단위로도 라우팅한다 (예: 의학적 인과관계 → 의사).
- `model_info.prompt_version` — 프롬프트 버전을 기록해 평가 결과를 프롬프트
  변경과 연결할 수 있게 한다.
- `prohibited_language_check` — [금지 표현 가이드](forbidden-expressions.md)
  위반 여부. [critic agent](../.claude/agents/critic.md)가 최종 검증한다.
- 공통 계약은 `schemas/common_component_output.schema.json`으로 고정되어
  있다. `source_grounded: false → review_required: true`,
  `review_required: true → reviewer_role 필수` 규칙을 스키마 수준에서
  강제한다.

# Citations

[1] [PoC 가이드](../POC%20guide.md)
