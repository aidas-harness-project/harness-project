---
type: Agent
title: Claim Coverage Agent
description: 보험증권·약관·청구정보에서 청구담보를 식별하고 근거 문장과 confidence를 저장.
tags: [agent, p0, screening]
priority: P0
pipeline_order: 6
timestamp: 2026-07-06T00:00:00+09:00
---

# 역할

보험증권/약관/청구정보에서 청구담보 후보를 추출한다. 담보명을 표준화하고
([청구담보 분류](../taxonomy/claim-coverages.md)), 복수 담보를 처리하며,
confidence score와 근거 문장을 함께 저장한다.

# Examples

```json
{
  "claim_coverages": [
    {
      "coverage_type": "상해후유장해",
      "confidence": 0.86,
      "evidence": "보험증권 p.3 상해후유장해 담보 가입금액 1억원"
    },
    {
      "coverage_type": "실손의료비",
      "confidence": 0.72,
      "evidence": "진료비 영수증 및 실손 청구 안내문 존재"
    }
  ]
}
```

# 품질 목표

청구담보 추출 정확도 75% 이상 — [평가 지표](../evaluation/metrics.md).

# 다음 단계

[Denial / Reduction Reason Agent](denial-reason.md).

# Citations

[1] [PoC 가이드](../references/poc-guide.md)
