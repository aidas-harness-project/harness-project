---
type: Agent
title: Evidence Check / Critic Agent
description: 초안 문장별 근거를 연결하고 근거 없는 주장·과도한 법률/의료 표현에 검수 필요 태그를 부여.
tags: [agent, p1, review]
priority: P1
pipeline_order: 13
timestamp: 2026-07-06T00:00:00+09:00
---

# 역할

초안의 문장마다 근거 문서를 연결하고, 다음을 탐지해 "검수 필요" 태그를 단다:

- 근거 없는 주장.
- 과도한 법률 판단 표현.
- 과도한 의료 확정 표현.

검수를 통과한 결과가 최종 초안 v1이다. 위험 표현과 대체 표현 목록은
[금지 표현 가이드](../templates/forbidden-expressions.md) 참고.

# 존재 이유

초안에 환각이 많아 검수 비용이 더 들면 PoC는
[No-Go](../evaluation/go-no-go.md)다. Critic Agent는 이를 막는 마지막 방어선이다.

# 다음 단계

[Evaluation Harness](evaluation-harness.md).

# Citations

[1] [PoC 가이드](../references/poc-guide.md)
