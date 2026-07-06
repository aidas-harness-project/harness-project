---
type: Taxonomy
title: 청구담보 분류
description: 청구담보 표준화 목록 — 실손의료비, 수술비, 진단비, 상해후유장해 등.
tags: [taxonomy, 청구담보]
timestamp: 2026-07-06T00:00:00+09:00
---

[Claim Coverage Agent](../agents/claim-coverage.md)가 담보명을 표준화할 때
쓰는 분류. 하나의 케이스에 복수 담보가 존재할 수 있다.

# 담보

표준 코드는 [I/O 계약](../sources/pipeline-io-contracts.md)의
`normalized_coverage_type` 필드에서 사용한다 (`injury_disability`,
`medical_expense`는 계약 초안에서 확정, 나머지는 제안값).

| 표준 담보명 | 표준 코드 | 비고 |
| --- | --- | --- |
| 실손의료비 | `medical_expense` | 급여/비급여 구분 검토 필요 |
| 수술비 | `surgery_benefit` (제안) | 수술 정의 충족 여부가 쟁점이 되기도 함 |
| 진단비 | `diagnosis_benefit` (제안) | 진단 확정 요건·검사 방법이 쟁점 (예: [뇌혈관질환진단비](../cases/coverage-dispute.md)) |
| 상해후유장해 | `injury_disability` | 장해지급률 × 가입금액으로 산정 |
| 질병후유장해 | `disease_disability` (제안) | 상해와 구분 필요 |
| 배상책임 | `liability` (제안) | 대인/대물 |

이 목록은 PoC 진행 중 케이스에서 확인되는 담보로 확장한다.

# Citations

[1] [PoC 가이드](../references/poc-guide.md)
