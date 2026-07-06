---
type: Architecture
title: Agent Harness 파이프라인
description: 케이스 입력부터 평가까지 14단계로 구성된 Agent Harness 전체 처리 흐름.
tags: [pipeline, agent-harness]
timestamp: 2026-07-06T00:00:00+09:00
---

# 전체 흐름

Case Pack 입력이 아래 순서로 처리된다. 각 단계는 독립 에이전트 개념으로
문서화되어 있다.

| 순서 | 단계 | 주요 출력 |
| --- | --- | --- |
| 1 | [Document Intake Agent](agents/document-intake.md) | 문서 목록 |
| 2 | [OCR / Text Extraction Layer](agents/ocr-layer.md) | 문서별 raw text |
| 3 | [Redaction Agent](agents/redaction.md) | 가명처리 텍스트 |
| 4 | [Document Classification Agent](agents/document-classification.md) | 진단서/약관/안내문 등 |
| 5 | [Field Extraction Agent](agents/field-extraction.md) | 진단명, KCD, 사고일 등 |
| 6 | [Claim Coverage Agent](agents/claim-coverage.md) | 실손, 수술비, 후유장해 등 |
| 7 | [Denial / Reduction Reason Agent](agents/denial-reason.md) | 기왕증, 약관 제한 등 |
| 8 | [Consistency Check Agent](agents/consistency-check.md) | 불일치 플래그 |
| 9 | [Case Type Classification Agent](agents/case-type.md) | 후유장해/실손/수술비 등 |
| 10 | [Policy Mapping Agent](agents/policy-mapping.md) | 약관 조항 리스트 |
| 11 | [Rebuttal Point Agent](agents/rebuttal.md) | 반박 논거 후보 |
| 12 | [Draft Writer Agent](agents/draft-writer.md) | draft_report.md |
| 13 | [Evidence Check / Critic Agent](agents/critic.md) | 검수 필요 표시 |
| 14 | [Evaluation Harness](agents/evaluation-harness.md) | 평가 리포트 |

# 우선순위

- **P0** (Step 1~2 필수): OCR/텍스트 추출, 문서 유형 분류, 핵심항목 추출,
  청구담보 추출, 부지급·감액사유 추출, 사건 유형 분류.
- **P1** (Step 2~3): 문서 간 불일치 탐지, 약관 조항 후보 매핑, 반박 포인트
  생성, 손사서 초안 구조·v1 작성.

# Citations

[1] [PoC 가이드](references/poc-guide.md)
