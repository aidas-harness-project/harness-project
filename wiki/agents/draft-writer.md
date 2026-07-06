---
type: Agent
title: Draft Writer Agent
description: 스크리닝 결과와 반박 포인트를 사건 유형별 손사서 목차에 채워 초안을 생성.
tags: [agent, p1, draft]
priority: P1
pipeline_order: 12
timestamp: 2026-07-06T00:00:00+09:00
---

# 역할

사건 유형별 손사서 목차 템플릿(후유장해형/실손·비급여형/진단·수술비형)을
선택하고, 스크리닝 결과를 손사서 구조로 변환한 뒤 초안 본문을 생성한다.

# 입력

- [스크리닝 리포트](../templates/screening-report.md) 결과 전체.
- [Rebuttal Point Agent](rebuttal.md)의 반박 논거.
- [Case Type Agent](case-type.md)의 사건 유형.

# 출력

`draft_report.md` — 구조는 [손사서 초안 템플릿](../templates/draft-report.md) 참고.

# 품질 목표

손사서 초안 생성 성공률 70% 이상, 백지 작성 대비 시간 절감 —
[평가 지표](../evaluation/metrics.md).

# 다음 단계

[Evidence Check / Critic Agent](critic.md)의 검수를 거쳐 초안 v1이 된다.

# Citations

[1] [PoC 가이드](../references/poc-guide.md)
