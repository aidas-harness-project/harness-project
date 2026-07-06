---
type: Decision Criteria
title: Go / No-Go 기준
description: 3주 PoC 종료 시 다음 단계 진행 여부를 판정하는 조건 목록.
tags: [evaluation, decision]
timestamp: 2026-07-06T00:00:00+09:00
---

# Go — 아래를 만족하면 다음 단계 진행

- 스크리닝 리포트가 전문가 검토의 출발점으로 쓸 만하다.
- 청구담보·감액사유 추출이 대체로 맞다.
- 약관 매핑이 완벽하지 않아도 후보 추천으로 유용하다.
- 손사서 초안이 백지 작성보다 시간을 줄인다.
- 실패 유형이 명확하고 개선 가능하다.

# No-Go — 아래에 해당하면 범위 재설계

- OCR 품질 때문에 문서 이해가 거의 불가능하다. → [OCR Layer](../agents/ocr-layer.md)
- 청구담보와 감액사유가 반복적으로 틀린다. → [Claim Coverage](../agents/claim-coverage.md), [Denial Reason](../agents/denial-reason.md)
- 약관 매핑이 무작위 수준이다. → [Policy Mapping](../agents/policy-mapping.md)
- 초안이 환각이 많아 검수 비용이 더 든다. → [Critic Agent](../agents/critic.md)
- 전문가가 "실무 보조도구로 사용하기 어렵다"고 평가한다.

# Citations

[1] [PoC 가이드](../references/poc-guide.md)
