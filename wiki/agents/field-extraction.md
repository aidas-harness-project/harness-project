---
type: Agent
title: Field Extraction Agent
description: 진단명, KCD, 사고일, 치료기간, 수술명, 병원명 등 핵심항목을 구조화 JSON으로 추출.
tags: [agent, p0, extraction]
priority: P0
pipeline_order: 5
timestamp: 2026-07-06T00:00:00+09:00
---

# 역할

문서 유형별로 아래 핵심항목을 추출해 JSON으로 저장한다.

# 추출 대상

- 진단명
- KCD 코드
- 사고일 / 발병일
- 수술명
- 치료기간
- 병원명
- 보험사 안내문상 부지급/감액 표현 후보 — [Denial Reason Agent](denial-reason.md)의 입력이 된다.

# 품질 목표

핵심항목 추출 정확도 80% 이상 — [평가 지표](../evaluation/metrics.md) 참고.

# 다음 단계

[Claim Coverage Agent](claim-coverage.md).

# Citations

[1] [PoC 가이드](../references/poc-guide.md)
