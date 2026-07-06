---
type: Agent
title: Rebuttal Point Agent
description: 감액사유별 반박 프레임에 따라 약관·의무기록 근거를 연결한 반박 논거 후보를 생성.
tags: [agent, p1, rebuttal]
priority: P1
pipeline_order: 11
timestamp: 2026-07-06T00:00:00+09:00
---

# 역할

감액사유별 반박 프레임을 정의하고, 약관 조항과 감액사유를 연결하고,
의무기록 근거를 연결해 반박 논거 후보를 생성한다. **근거 없는 반박은
"검수 필요" 처리**하고 케이스별 반박 포인트 리포트를 만든다.

# 입력

- [Denial Reason Agent](denial-reason.md)의 감액사유.
- [Policy Mapping Agent](policy-mapping.md)의 약관 조항 후보.
- 의무기록·진단서 추출 결과.

# 출력

[반박 포인트 리포트](../templates/rebuttal-points.md) 형식의 케이스별 리포트.

# 다음 단계

[Draft Writer Agent](draft-writer.md) — 반박 논거가 초안 §6
(감액/부지급 사유에 대한 검토)에 들어간다.

# Citations

[1] [PoC 가이드](../references/poc-guide.md)
