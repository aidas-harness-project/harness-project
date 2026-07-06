---
type: Agent
title: Denial / Reduction Reason Agent
description: 보험사 안내문에서 감액·부지급 문구를 추출하고 taxonomy 코드로 분류.
tags: [agent, p0, screening]
priority: P0
timestamp: 2026-07-06T00:00:00+09:00
---

# 역할

보험사 안내문에서 감액·부지급 문구를 추출하고
[감액사유 Taxonomy](../taxonomy/reduction-reasons.md) (R01~R99)로 분류한다.
감액 금액 또는 지급 제외 금액을 추출하고 근거 문장을 저장한다.

# 대표 감액사유

기왕증, 장해율 과다, 약관상 지급요건 미충족, 면책사항, 치료 필요성 부족,
서류 부족 등. 전체 코드는 [감액사유 Taxonomy](../taxonomy/reduction-reasons.md) 참고.

# Phase 소속과 실행 시점

아키텍처상 [Phase 2](../pipeline.md)(반려·감액 대응) 소속이지만, 종결
케이스 PoC에서는 보험사 안내문이 처음부터 케이스 팩에 있고
[스크리닝 리포트](../templates/screening-report.md) §2가 감액사유를
요구하므로 **Week 2에 당겨 실행**한다. 구현은 `DenialResponseAgent` 묶음
(사유 추출 + taxonomy 분류 + 약관 매칭).

# 품질 목표

감액사유 Top-3 일치율 75% 이상 — [평가 지표](../evaluation/metrics.md).

# 다음 단계

[Consistency Check Agent](consistency-check.md). 추출된 감액사유는
이후 [Rebuttal Point Agent](rebuttal.md)의 핵심 입력이 된다.

# Citations

[1] [PoC 가이드](../references/poc-guide.md)
