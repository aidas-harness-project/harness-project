---
type: Agent
title: Document Classification Agent
description: 문서를 진단서·의무기록·약관·안내문 등 유형으로 분류하고 confidence를 저장.
tags: [agent, p0, classification]
priority: P0
pipeline_order: 4
timestamp: 2026-07-06T00:00:00+09:00
---

# 역할

가명처리 텍스트를 [문서 유형 분류 체계](../taxonomy/document-types.md)에 따라
분류한다. 분류 confidence score를 함께 저장한다.

# 입력 / 출력

- 입력: [Redaction Agent](redaction.md)의 가명처리 텍스트.
- 출력: 문서별 유형 라벨 + confidence.

# 다음 단계

[Field Extraction Agent](field-extraction.md) — 문서 유형에 따라
추출 대상 필드가 달라진다.

# Citations

[1] [PoC 가이드](../references/poc-guide.md)
