---
type: Architecture
title: 프로젝트 폴더 구조
description: 하네스 규약(.claude 에이전트·스킬)과 파이프라인 경로 규약(data → outputs)을 결합한 프로젝트 폴더 구조와 정답지 격리 설계.
tags: [structure, harness, pipeline]
timestamp: 2026-07-07T00:00:00+09:00
---

[파이프라인](pipeline.md)의 경로 규약과 하네스 규약(에이전트 정의 +
스킬 + 오케스트레이터)을 결합한 구조. 2026-07-07 스캐폴딩 완료
(에이전트/스킬 정의는 후속 작업).

# 구조

| 경로 | 계층 | 용도 |
| --- | --- | --- |
| `POC/`, `sources/`, `case_qna.pdf` | raw (불변) | 케이스 원자료·ingest 원자료. 읽기 전용 |
| `wiki/` | 지식 | LLM Wiki — 설계·도메인 지식 ([Wiki Schema](CLAUDE.md)) |
| `schemas/` | 계약 | JSON Schema v0.1+ — 컴포넌트 I/O 검증 게이트 |
| `tools/` | 도구 | `wiki_lint.py` + (예정) `validate_output.py` |
| `.claude/agents/` | 하네스 | 7개 묶음 Agent 정의 ([파이프라인](pipeline.md)의 구현 묶음과 1:1) |
| `.claude/skills/` | 하네스 | 오케스트레이터 + 에이전트별 작업 스킬 (프롬프트는 `assets/`에 버전 관리 → [출력 계약](templates/component-output.md)의 `prompt_version`과 연동) |
| `data/raw/CASE_XXX/` | 실행 (gitignore) | Case Intake가 `POC/`에서 복사한 모델 입력용 사본 — **정답지 제외** |
| `data/processed/CASE_XXX/DOC_XXX/` | 실행 (gitignore) | 중간 산출물 (`page_*.md`, `redacted_text.md`, chunks) |
| `data/ground_truth/CASE_XXX/` | 실행 (gitignore) | **정답지 격리** — 최종 손사서·지급결과. `critic-evaluation` 에이전트만 접근 |
| `outputs/CASE_XXX/` | 산출물 (커밋) | backbone 5 + 리포트·초안. 스키마 검증 통과분만 |
| `outputs/evaluation_summary.json` | 산출물 | 케이스 집계 평가 리포트 ([갭 2 계획](answers/pipeline-understanding-and-gap-plan.md)) |
| `_workspace/` | 임시 (gitignore) | 하네스 실행별 조율 산출물 (`{phase}_{agent}_{artifact}` 규약) |

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
