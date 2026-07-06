---
type: Taxonomy
title: 감액사유 Taxonomy v1
description: 보험사 감액·부지급 사유를 R01~R99 코드로 표준화한 분류 체계.
tags: [taxonomy, 감액사유]
timestamp: 2026-07-06T00:00:00+09:00
---

[Denial / Reduction Reason Agent](../agents/denial-reason.md)가 감액·부지급
문구를 아래 코드로 분류한다.

# 코드

| 코드 | 감액사유 |
| --- | --- |
| R01 | 기왕증 / 기존 질환 기여도 |
| R02 | 장해율 과다 |
| R03 | 손해액 과다 |
| R04 | 약관상 지급요건 미충족 |
| R05 | 면책사항 |
| R06 | 치료 필요성 부족 |
| R07 | 과잉진료 / 비급여 적정성 |
| R08 | 서류 부족 |
| R09 | 동일 사유 재청구 |
| R99 | 기타 / 분류 불가 |

# 케이스 대응 예시

- R01 (기왕증 기여도): [골다공증 기여도 감액 케이스](../cases/preexisting-condition.md)
  — 골다공증 기여도로 장해지급률 10%포인트 공제.
- R04/R05 (지급요건/면책): [약관상 지급범위 분쟁 케이스](../cases/coverage-dispute.md)
  — 뇌혈관질환진단비 지급범위를 두고 보험사가 면책 주장.

# Citations

[1] [PoC 가이드](../references/poc-guide.md)
