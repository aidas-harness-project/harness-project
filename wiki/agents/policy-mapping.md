---
type: Agent
title: Policy Mapping Agent
description: 담보명·감액사유 기반으로 관련 약관 조항 후보 리스트를 검색해 제시.
tags: [agent, p1, policy]
priority: P1
pipeline_order: 10
timestamp: 2026-07-06T00:00:00+09:00
---

# 역할

약관 텍스트를 chunking한 뒤, 담보명 기반·감액사유 기반으로 조항 후보를
검색해 근거 문장과 함께 약관 조항 리스트를 반환한다. 완벽한 매핑이
목표가 아니라 손사 검토의 **출발점이 되는 후보 추천**이 목표다.

# 세부 컴포넌트 분해

[파이프라인 개편](../pipeline.md) 이후 다음 컴포넌트로 세분화됐다
(구현은 `PolicyPipelineAgent` 묶음):

1. **Policy Document Processing** — 약관을 조항 단위로 구조화.
2. **Policy Clause Extraction** — 지급요건·면책·감액 관련 조항 추출.
3. **Policy Clause Normalization** — 조항을 표준 필드로 정규화.
4. **Requirement Matching** — 담보별 지급요건과 청구 자료 매칭 (Phase 1).
5. **Policy-to-Denial Matching** — 반려사유와 조항 연결 (Phase 2).

검색 인프라는 벡터 인덱싱 없이 직접 프롬프팅/BM25로 시작한다
(케이스 수가 적은 PoC에서는 과투자 — 규모 확대 시 도입).

# 입력

- [Claim Coverage Agent](claim-coverage.md)의 청구담보.
- [Denial Reason Agent](denial-reason.md)의 감액사유.
- 케이스 내 약관 문서 텍스트.

# 품질 목표

관련 조항 Top-3 포함률 60~70% 이상 — [평가 지표](../evaluation/metrics.md).
무작위 수준이면 [No-Go](../evaluation/go-no-go.md) 조건이다.

# 다음 단계

[Rebuttal Point Agent](rebuttal.md).

# Citations

[1] [PoC 가이드](../references/poc-guide.md)
