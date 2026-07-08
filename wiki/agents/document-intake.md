---
type: Agent
title: Document Intake Agent
description: 케이스 폴더의 파일을 수집·정렬해 문서 목록을 생성하는 파이프라인 진입점.
tags: [agent, p0, intake]
priority: P0
pipeline_order: 1
timestamp: 2026-07-07T00:00:00+09:00
---

# 역할

Case Pack(케이스 폴더)에 들어 있는 PDF·이미지·텍스트 파일을 수집하고
정렬해 이후 단계가 순회할 문서 목록을 만든다.

# 입력 / 출력

- 입력: 케이스 폴더 경로. 예: [케이스 목록](../cases/index.md)의 각 케이스.
- 출력: 문서 목록 (파일명, 형식, 크기).

# 구현 (tools/intake_case.py)

정답지 격리 복사 스크립트가 이 단계의 앞부분을 담당한다. dry-run이
기본이고 사람이 분류를 확인한 뒤 `--yes`로 실행한다.

- **페이지 범위 분할 `--split`** (2026-07-07 추가): 한 PDF에 여러 문서
  (손해사정서 본문 + 증빙자료 등)가 묶여 있으면 "파일=문서" 가정이
  깨진다. `--split "파일명:1-13=ground_truth,14-14=raw,..."`로 문서
  경계 단위 분할 복사한다. 경계는 페이지 렌더링 육안 스캔으로 확정하고
  (증빙자료 목차가 대조 기준), dry-run으로 사람 확인을 거친다. 분할
  내역은 `_intake_record.json`의 `splits`에 기록된다. 첫 적용:
  [CASE_003](../cases/permanent-disability.md).
- **파일 선별 `--files`**: 한 폴더에 여러 케이스가 섞였을 때 패턴으로
  대상 파일만 고른다 (CASE_003/004 분리에 사용).
- manifest(`document_manifest.json`) 생성은 오케스트레이터가 분할된
  raw 파일 목록으로 수행한다 (owner: CaseIntake 필드).

# 다음 단계

[OCR / Text Extraction Layer](ocr-layer.md)로 문서 목록을 전달한다.

# Citations

[1] [PoC 가이드](../references/poc-guide.md)
