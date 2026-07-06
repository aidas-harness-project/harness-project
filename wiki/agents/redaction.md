---
type: Agent
title: Redaction Agent
description: OCR 텍스트에서 민감정보를 제거해 가명처리 텍스트를 만드는 에이전트.
tags: [agent, p0, privacy]
priority: P0
pipeline_order: 3
timestamp: 2026-07-06T00:00:00+09:00
---

# 역할

이름, 주민번호, 연락처 등 민감정보를 제거·치환해 이후 단계가 다루는
텍스트를 가명처리 상태로 유지한다. PoC 입력 케이스는 이미 "고객정보 삭제"
처리가 되어 있으나, OCR 결과에 잔존 정보가 남을 수 있어 이중 방어로 둔다.

# 입력 / 출력

- 입력: [OCR Layer](ocr-layer.md)의 문서별 raw text.
- 출력: 가명처리 텍스트.

# 다음 단계

[Document Classification Agent](document-classification.md).

# Citations

[1] [PoC 가이드](../references/poc-guide.md)
