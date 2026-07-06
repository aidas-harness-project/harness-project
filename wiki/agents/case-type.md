---
type: Agent
title: Case Type Classification Agent
description: 케이스를 후유장해·진단/수술비·실손·배상책임 등 사건 유형으로 분류.
tags: [agent, p0, screening]
priority: P0
pipeline_order: 9
timestamp: 2026-07-06T00:00:00+09:00
---

# 역할

케이스 전체를 [사건 유형 분류 체계](../taxonomy/case-types.md)에 따라
분류한다. 사건 유형은 [손사서 초안 템플릿](../templates/draft-report.md)
선택(후유장해형/실손·비급여형/진단·수술비형)의 기준이 된다.

# 품질 목표

사건 유형 분류 정확도 80% 이상 — [평가 지표](../evaluation/metrics.md).

# 다음 단계

[Policy Mapping Agent](policy-mapping.md).

# Citations

[1] [PoC 가이드](../references/poc-guide.md)
