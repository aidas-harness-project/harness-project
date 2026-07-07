---
type: Agent
title: Field Extraction Agent
description: 진단명, KCD, 사고일, 치료기간, 수술명, 병원명 등 핵심항목을 구조화 JSON으로 추출.
tags: [agent, p0, extraction]
priority: P0
pipeline_order: 5
timestamp: 2026-07-07T00:00:00+09:00
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
- ~~보험사 안내문상 부지급/감액 표현 후보~~ — 파이프라인 개편 후
  `DenialResponseAgent`가 안내문에서 **직접 추출**하는 것으로 이관됨
  (`denial_reason_result.json`). 이 에이전트의 산출물
  (`extracted_claim_fields.json`, `schemas/`에 v0.1 스키마)에는 포함하지
  않는다 — [Denial Reason Agent](denial-reason.md) 참고.

# 품질 목표

핵심항목 추출 정확도 80% 이상 — [평가 지표](../evaluation/metrics.md) 참고.

# 다음 단계

[Claim Coverage Agent](claim-coverage.md).

# Citations

[1] [PoC 가이드](../references/poc-guide.md)
