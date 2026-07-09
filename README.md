# 손해사정 Agent Harness PoC

가명처리된 종결 손해사정 케이스 원자료를 넣으면, 7개 전문 에이전트로 구성된
Agent Harness가 **스크리닝 리포트**와 **손해사정서 초안**을 자동 생성하고,
실제 최종 손사서(정답지)와 비교 평가하는 3주 PoC 실험입니다. 모델을 새로
학습하지 않고 기존 OCR/LLM/검색 모델을 조합해 검증합니다.

자세한 설계·의사결정 근거는 이 저장소에 함께 있는 [`wiki/`](wiki/index.md)
Obsidian vault에 있습니다. 이 README는 처음 저장소를 여는 사람이 전체 그림을
빠르게 잡기 위한 진입점입니다.

## 핵심 질문

1. 기존 모델만으로 보험 청구 관련 문서를 구조화할 수 있는가?
2. 청구담보와 감액사유를 실무적으로 쓸 만한 수준으로 추출할 수 있는가?
3. 문서 간 불일치와 핵심 쟁점을 잡아낼 수 있는가?
4. 약관 조항 후보를 찾아 손사 검토의 출발점으로 쓸 수 있는가?
5. 감액사유별 반박 포인트와 손사서 초안이 실제 업무 시간을 줄여주는가?

성공 판정 기준은 [평가 지표](wiki/evaluation/metrics.md)와
[Go/No-Go 기준](wiki/evaluation/go-no-go.md)에 정리되어 있습니다.

## 폴더 구조

| 경로 | 계층 | 용도 |
| --- | --- | --- |
| `POC/`, `sources/`, `case_qna.pdf` | raw (불변) | 케이스 원자료·참고자료. **읽기 전용** |
| `wiki/` | 지식 | 설계·도메인 지식을 담은 Obsidian vault (LLM Wiki) |
| `schemas/` | 계약 | 컴포넌트 I/O를 검증하는 JSON Schema |
| `tools/` | 도구 | `intake_case.py`(정답지 격리 복사), `validate_output.py`(스키마 검증), `wiki_lint.py`(위키 무결성 검사) |
| `.claude/agents/` | 하네스 | 7개 전문 에이전트 정의 |
| `.claude/skills/` | 하네스 | 오케스트레이터(`loss-adjustment-pipeline`) + 공통 출력 계약(`component-output-contract`) |
| `data/raw/CASE_XXX/` | 실행 (gitignore) | Case Intake가 `POC/`에서 복사한 **모델 입력용** 사본 (정답지 제외) |
| `data/processed/CASE_XXX/DOC_XXX/` | 실행 (gitignore) | OCR·가명처리·청킹 등 중간 산출물 |
| `data/ground_truth/CASE_XXX/` | 실행 (gitignore) | **정답지 격리** — 최종 손사서·지급결과. `critic-evaluation` 에이전트만 접근 |
| `outputs/CASE_XXX/` | 산출물 (커밋) | 스크리닝 리포트, 손사서 초안, 평가 결과 등 최종 산출물 |
| `_workspace/RUN_YYYYMMDD_NNN/` | 임시 (gitignore) | 실행 단위 조율 노트(애매한 판단·에러·다음 단계 맥락) |

폴더 구조 설계 원칙 상세는 [`wiki/project-structure.md`](wiki/project-structure.md)를 참고하세요.

## 파이프라인 개요

Phase 1(최초 청구/검토)과 Phase 2(보험사 반려·감액 대응)로 나뉘며, 7개
묶음 에이전트가 각 단계를 담당합니다.

| 에이전트 | 역할 |
| --- | --- |
| `document-pipeline` | OCR, 가명처리, 문서 유형 분류, 전처리(청킹) |
| `policy-pipeline` | 약관 조항 추출·정규화 |
| `claim-analysis` | 핵심항목 추출, 청구담보 식별, 사건 유형 분류, 지급요건 매칭 |
| `evidence-validation` | 근거 검증, 문서 간 불일치 탐지 |
| `denial-response` | 감액·부지급 사유 추출·분류, 약관 매칭, 반박 포인트 생성 |
| `report-generation` | 스크리닝 리포트, 손사서 초안 v1/v2 생성 |
| `critic-evaluation` | 금지 표현 검수, 정답지 대비 평가 (**정답지 접근 권한 유일 보유**) |

각 에이전트의 입출력 파일과 실행 순서는 오케스트레이터 스킬
(`.claude/skills/loss-adjustment-pipeline/SKILL.md`)이 정의하며, 서브에이전트는
서로의 컨텍스트를 공유하지 않고 **파일시스템을 데이터 버스**로 삼아
연결됩니다. 이 실행 메커니즘의 상세는
[`wiki/orchestration.md`](wiki/orchestration.md), 단계별 I/O 표는
[`wiki/pipeline.md`](wiki/pipeline.md)에 정리되어 있습니다.

## 정답지 격리 (가장 중요한 설계 원칙)

`POC/` 케이스 폴더에는 모델 입력용 문서와 평가용 최종 손사서(정답지)가
섞여 있습니다. Case Intake(`tools/intake_case.py`)가 `data/raw/`로 복사할 때
정답지를 제외하고 `data/ground_truth/`로 분리하여, **정답지가 모델 입력으로
새어 들어가지 않도록 폴더 구조 수준에서 강제**합니다.

- `data/ground_truth/`는 `critic-evaluation` 에이전트만 접근합니다.
- 파이프라인의 다른 모든 에이전트·스킬·오케스트레이터는 이 폴더를 열지 않습니다.
- intake 시 정답지 분류는 dry-run으로 사용자 확인을 거친 뒤 `--yes`로 확정합니다.

## 케이스 목록

| 케이스 | 개요 |
| --- | --- |
| CASE_003 | 보도블럭 전도사고로 인한 상완골 근위부 골절 수술 후 영조물 배상책임 공제로 손해사정된 후유장해 케이스 |
| CASE_004 | 좌측 발목 삼복사 골절(S82.830)로 한화손보 상대 배상책임 손해사정(사정금액 약 1.03억) |
| CASE_005 | 척추 장해지급률 50% 해당 사례에서 골다공증 기여도 10%p 공제로 40% 인정된 기왕증 감액 케이스 (R01) |
| CASE_006 | 뇌혈관질환진단비 약관상 지급범위를 두고 4개 보험사와 다툰 케이스 (R04/R05) |

케이스별 상세는 [`wiki/cases/index.md`](wiki/cases/index.md)를 참고하세요.

## 실행 방법

Claude Code에서 이 저장소를 열고 자연어로 요청하면 `loss-adjustment-pipeline`
스킬이 자동으로 오케스트레이터 역할을 맡아 7개 에이전트를 순서대로 호출합니다.

```
"CASE_003 처리해줘"          → 초기 실행 (intake부터 평가까지)
"CASE_003 스크리닝만 다시"    → 부분 재실행
"CASE_003 평가 돌려줘"        → 정답지와 비교해 Go/No-Go 판정 자료 생성
```

각 단계 완료 시 `python tools/validate_output.py <파일.json>`으로 스키마
검증 게이트를 통과해야 다음 단계로 진행됩니다. 실행 모드 판별(초기/전체
재실행/부분 재실행/중단 이어받기)과 단계별 상세 규칙은
`.claude/skills/loss-adjustment-pipeline/SKILL.md`가 정본입니다.

## 도구

| 명령 | 용도 |
| --- | --- |
| `python tools/intake_case.py "<POC 케이스 폴더>" CASE_XXX` | 원자료 intake — `data/raw/`와 `data/ground_truth/` 분리 복사 (dry-run 기본, `--yes`로 확정) |
| `python tools/validate_output.py <파일.json>` | 산출물의 JSON Schema 준수 여부 검증 |
| `python tools/wiki_lint.py` | 위키 문서 구조·링크 무결성 검사 |

## 더 알아보기

- 전체 지식 베이스: [`wiki/index.md`](wiki/index.md)
- 파이프라인 설계 정본: [`wiki/pipeline.md`](wiki/pipeline.md)
- 실행 런타임 메커니즘: [`wiki/orchestration.md`](wiki/orchestration.md)
- 폴더 구조·정답지 격리 설계: [`wiki/project-structure.md`](wiki/project-structure.md)
- 평가 지표·Go/No-Go 기준: [`wiki/evaluation/index.md`](wiki/evaluation/index.md)
- 세션 진행 시 필수로 지킬 규칙: [`AGENTS.md`](AGENTS.md) / [`CLAUDE.md`](CLAUDE.md)
