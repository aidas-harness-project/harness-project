---
type: Agent
title: OCR / Text Extraction Layer
description: PDF·이미지에서 페이지 단위 텍스트를 추출하고 OCR 품질 로그를 남기는 계층.
tags: [agent, p0, ocr]
priority: P0
pipeline_order: 2
timestamp: 2026-07-06T00:00:00+09:00
---

# 역할

PDF 내장 텍스트 추출과 이미지 OCR을 연결해 문서별 raw text를 만든다.

# 요구사항

- 페이지 단위로 텍스트 저장.
- 문서별 OCR 품질 로그 생성.
- OCR 실패 문서 표시 — 실패율이 높으면 [Go/No-Go 기준](../evaluation/go-no-go.md)의
  No-Go 조건("OCR 품질 때문에 문서 이해가 거의 불가능하다")에 해당할 수 있다.

# 주의

케이스 원자료 중 일부 텍스트 파일은 CP949(EUC-KR) 인코딩이다. UTF-8로
가정하고 읽으면 깨지므로 인코딩 감지가 필요하다.

# 다음 단계

[Redaction Agent](redaction.md).

# Citations

[1] [PoC 가이드](../references/poc-guide.md)
