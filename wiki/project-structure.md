---
type: Architecture
title: 프로젝트 폴더 구조
description: 하네스 규약(.claude 에이전트·스킬)과 파이프라인 경로 규약(data → outputs)을 결합한 프로젝트 폴더 구조와 정답지 격리 설계.
tags: [structure, harness, pipeline]
timestamp: 2026-07-07T00:00:00+09:00
---

[파이프라인](pipeline.md)의 경로 규약과 하네스 규약(에이전트 정의 +
스킬 + 오케스트레이터)을 결합한 구조. 2026-07-07 스캐폴딩 및 하네스 구축
완료 (에이전트 7 + 스킬 2 + 도구 2).

# 구조

| 경로 | 계층 | 용도 |
| --- | --- | --- |
| `POC/`, `sources/`, `case_qna.pdf` | raw (불변) | 케이스 원자료·ingest 원자료. 읽기 전용 |
| `wiki/` | 지식 | LLM Wiki — 설계·도메인 지식 ([Wiki Schema](CLAUDE.md)) |
| `schemas/` | 계약 | JSON Schema v0.1+ — 컴포넌트 I/O 검증 게이트 |
| `tools/` | 도구 | `wiki_lint.py`, `validate_output.py`(스키마 검증 게이트), `intake_case.py`(정답지 격리 복사, 기본 dry-run) |
| `.claude/agents/` | 하네스 | 7개 묶음 Agent 정의 ([파이프라인](pipeline.md)의 구현 묶음과 1:1): document-pipeline, policy-pipeline, claim-analysis, evidence-validation, denial-response, report-generation, critic-evaluation |
| `.claude/skills/` | 하네스 | `loss-adjustment-pipeline`(오케스트레이터 — 실행 순서·게이트·에러 핸들링) + `component-output-contract`(전 에이전트 공유 출력 규칙). 프롬프트 템플릿은 구현 시 스킬 `assets/`에 버전 관리 → [출력 계약](templates/component-output.md)의 `prompt_version`과 연동 |
| `data/raw/CASE_XXX/` | 실행 (gitignore) | Case Intake가 `POC/`에서 복사한 모델 입력용 사본 — **정답지 제외** |
| `data/processed/CASE_XXX/DOC_XXX/` | 실행 (gitignore) | 중간 산출물 (`page_*.md`, `redacted_text.md`, chunks) |
| `data/ground_truth/CASE_XXX/` | 실행 (gitignore) | **정답지 격리** — 최종 손사서·지급결과. `critic-evaluation` 에이전트만 접근 |
| `outputs/CASE_XXX/` | 산출물 (커밋) | backbone 5 + 리포트·초안. 스키마 검증 통과분만 |
| `outputs/evaluation_summary.json` | 산출물 | 케이스 집계 평가 리포트 ([갭 2 계획](answers/pipeline-understanding-and-gap-plan.md)) |
| `_workspace/` | 임시 (gitignore) | 하네스 실행별 조율 산출물 (`{phase}_{agent}_{artifact}` 규약) |

# _workspace 규약

에이전트별로 하위 구조를 달리하지 않는다 — **실행(run) 단위 폴더 + 파일명
규약**으로 통일한다:

- `_workspace/RUN_YYYYMMDD_NNN/` — 폴더명 = 공통 계약의 `run_id`.
  산출물 JSON의 `run_id`에서 해당 실행의 워크스페이스로 역추적한다.
- 파일명: `{순서}_{agent}_{artifact}.md` (예: `03_claim-analysis_notes.md`,
  `05_denial-response_errors.md`). notes는 필수, errors/handoff는 필요 시.
- 계약 산출물(JSON·리포트)은 여기 두지 않는다 — 진실의 원천은 `outputs/`와
  `data/processed/` 하나씩. _workspace에는 계약에 담기지 않는 것만
  (애매했던 판단, 에러 상황, 다음 단계용 맥락).
- 예외: 한 에이전트가 한 실행에서 5개 파일 이상 만들면 그 에이전트만
  `RUN_XXX/{agent}/` 하위 폴더를 판다.

# Python 코드 배치 규칙

- **`tools/`** — 여러 에이전트/사람이 공용으로 쓰는 범용 스크립트
  (검증 게이트, intake, 집계).
- **`.claude/skills/{스킬}/scripts/`** — 특정 스킬 워크플로 안에서만 도는
  실행 코드 (예: document-processing의 PDF 추출).
- 모델 학습 코드는 없다 — 기존 모델을 쓰는 PoC이므로 "모델 레이어"는
  에이전트 정의와 프롬프트 템플릿(버전 관리 파일)이다.

# 설계 원칙

1. **정답지 격리를 폴더 수준에서 강제** — `POC/`에는 모델 입력용 문서와
   평가용 정답지가 섞여 있다. Case Intake가 `data/raw/`로 복사할 때
   정답지를 제외하고 `data/ground_truth/`로 분리해, "정답지를 모델
   입력으로 쓰지 말 것" 규칙을 구조로 지킨다.
2. **스키마 검증 = 에이전트 경계 게이트** — 각 묶음 Agent의 경계 Output을
   `schemas/`로 검증하고 실패 시 다음 단계로 넘기지 않는다.
   [파이프라인](pipeline.md)의 "경계 Input/Output" 표가 통합 계약이다.
3. **wiki(설계) / .claude(실행) 분리** — 에이전트 정의(누가)와 스킬
   (어떻게)은 `.claude/`, 도메인 지식·설계 근거는 `wiki/`에 두고 서로
   참조만 한다.

# Citations

[1] [파이프라인 구조](pipeline.md)
[2] [컴포넌트별 I/O 계약 초안](sources/pipeline-io-contracts.md)
