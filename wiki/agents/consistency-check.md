---
type: Agent
title: Consistency Check Agent
description: 문서 간 날짜·진단명·사고경위·치료기간 불일치를 탐지하고 심각도를 점수화.
tags: [agent, p1, screening]
priority: P1
pipeline_order: 8
timestamp: 2026-07-06T00:00:00+09:00
---

# 역할

문서별 핵심항목을 교차 비교해 불일치를 탐지한다. 최소 범위는 날짜와
진단명이며, 사고경위·치료기간 불일치까지 확장한다. 불일치마다 심각도
점수와 검수 필요 여부를 부여한다.

# Examples

```json
{
  "case_type": "후유장해",
  "inconsistencies": [
    {
      "field": "accident_date",
      "doc_a": "진단서",
      "value_a": "2024-03-12",
      "doc_b": "보험사 안내문",
      "value_b": "2024-03-15",
      "severity": "medium",
      "review_required": true
    }
  ]
}
```

# 다음 단계

[Case Type Classification Agent](case-type.md). 불일치 플래그는
[스크리닝 리포트](../templates/screening-report.md)의 §4에 들어간다.

# Citations

[1] [PoC 가이드](../references/poc-guide.md)
