---
type: Agent
title: Document Intake Agent
description: 케이스 폴더의 파일을 수집·정렬해 문서 목록을 생성하는 파이프라인 진입점.
tags: [agent, p0, intake]
priority: P0
pipeline_order: 1
timestamp: 2026-07-06T00:00:00+09:00
---

# 역할

Case Pack(케이스 폴더)에 들어 있는 PDF·이미지·텍스트 파일을 수집하고
정렬해 이후 단계가 순회할 문서 목록을 만든다.

# 입력 / 출력

- 입력: 케이스 폴더 경로. 예: [케이스 목록](../cases/index.md)의 각 케이스.
- 출력: 문서 목록 (파일명, 형식, 크기).

# 다음 단계

[OCR / Text Extraction Layer](ocr-layer.md)로 문서 목록을 전달한다.

# Citations

[1] [PoC 가이드](../references/poc-guide.md)
