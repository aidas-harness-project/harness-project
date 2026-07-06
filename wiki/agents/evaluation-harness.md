---
type: Agent
title: Evaluation Harness
description: 모델 산출물을 실제 최종 손사서·지급 결과와 항목별로 비교해 평가 리포트를 생성.
tags: [agent, evaluation]
pipeline_order: 14
timestamp: 2026-07-06T00:00:00+09:00
---

# 역할

모델에는 숨겨둔 정답지(최종 손해사정서, 실제 지급/감액 회복 결과)와 모델
산출물을 항목별로 비교한다.

# 비교 항목

- 핵심항목 추출 정확도 (필드별 정답 일치율)
- 사건 유형 분류 정확도
- 감액사유 Top-1 / Top-3 일치율
- 약관 매핑 Top-3 포함 여부
- 손사서 초안 품질 루브릭 (손사·의사 1~5점 평가 입력 화면 포함)

전체 지표와 목표치는 [평가 지표](../evaluation/metrics.md), 판정 기준은
[Go/No-Go 기준](../evaluation/go-no-go.md) 참고.

# Citations

[1] [PoC 가이드](../references/poc-guide.md)
