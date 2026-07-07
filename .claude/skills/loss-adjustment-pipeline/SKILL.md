---
name: loss-adjustment-pipeline
description: 손해사정 Agent Harness 파이프라인의 오케스트레이터. 케이스 처리 실행("케이스 돌려줘", "CASE_001 처리", "파이프라인 실행", "스크리닝 리포트 만들어줘", "손사서 초안 생성"), 재실행·업데이트("다시 실행", "스크리닝만 다시", "초안 업데이트", "이전 결과 개선", "특정 단계만 재실행"), 평가 실행("평가 돌려줘", "정답지와 비교", "Go/No-Go 판정 자료") 요청 시 반드시 이 스킬을 사용할 것. 파이프라인 설계에 대한 단순 질문은 wiki/pipeline.md로 직접 답해도 된다.
---

# 손해사정 파이프라인 오케스트레이터

7개 전문 에이전트를 조율해 케이스를 스크리닝 리포트·손사서 초안·평가까지
처리한다. 설계 정본은 `wiki/pipeline.md`(단계별 I/O)와
`wiki/project-structure.md`(폴더 규약)다.

**실행 모드: 서브 에이전트 파이프라인 (하이브리드 아님).**
근거: 에이전트 간 통신이 전부 스키마 검증된 파일 계약으로 구조화되어
있어 실시간 팀 통신(SendMessage)의 이득이 없다. 각 에이전트는 결과
파일과 요약만 반환하면 되고, 병렬 구간은 `run_in_background`로 처리한다.
모든 Agent 호출에 해당 에이전트 정의(`.claude/agents/{name}.md`)를
subagent로 지정하고 `model: "opus"`를 명시한다.

## Phase 0: 컨텍스트 확인 (매 실행 시작 시)

1. `run_id` 결정: 새 실행이면 `RUN_{YYYYMMDD}_{NNN}` 발급,
   `_workspace/`의 기존 폴더로 순번 결정.
2. 실행 모드 판별:
   - `outputs/CASE_XXX/`에 산출물 존재 + 부분 수정 요청 → **부분 재실행**
     (해당 에이전트만, 하류 영향 단계 목록을 사용자에게 확인)
   - 산출물 존재 + 새 실행 요청 → 기존 `_workspace/RUN_XXX/`는 그대로 두고
     새 RUN 폴더로 **전체 재실행** (outputs는 덮어씀 — run_id로 구분)
   - 산출물 없음 → **초기 실행**
3. 입력 확인: `data/raw/CASE_XXX/`가 없으면 먼저 intake를 실행한다:
   `python tools/intake_case.py "<POC 케이스 폴더>" CASE_XXX` (dry-run 출력의
   정답지 분류를 **사용자에게 확인받은 뒤** `--yes`로 실행). 정답지 분류
   확인은 생략 불가 — 잘못 분류되면 평가가 오염된다.

## 실행 단계

각 단계 완료 시 게이트: `python tools/validate_output.py`로 경계 Output
검증 → PASS만 다음 단계 진행. 상세 I/O는 `wiki/pipeline.md`의 표 참조.

| 단계 | 에이전트 | 병렬 | 경계 Output (게이트 대상) |
| --- | --- | --- | --- |
| 0 | (스크립트) intake | - | `data/raw/`, `data/ground_truth/` 분리 |
| 1 | document-pipeline | - | manifest 갱신, `classification_result.json`, `page_chunks.json` |
| 2a | policy-pipeline | 2b와 병렬 | `normalized_policy_clause.json` |
| 2b | denial-response (추출·분류만) | 2a와 병렬 | `denial_reason_result.json` |
| 3 | claim-analysis | - | `extracted_claim_fields.json` 외 3개 |
| 4 | evidence-validation | - | `evidence_validation_result.json` |
| 5 | report-generation (스크리닝) | - | `screening_report.json`·`.md` |
| 6 | denial-response (약관 매칭·반박) | - | `policy_to_denial_matching_result.json`, `rebuttal_points.json` |
| 7 | report-generation (초안 v1→v2) | - | `draft_report_v1.md`, `draft_report_v2.md` |
| 8 | critic-evaluation (검수) | - | `critic_result.json`, reviewed 초안 |
| 9 | (사람) Human Review | - | `expert_review.json` |
| 10 | critic-evaluation (평가·집계) | - | `evaluation_result.json`, `evaluation_summary.json` |

- 2a/2b 병렬은 `run_in_background: true`로 동시 실행하고 둘 다 완료 후
  3으로 진행한다.
- 단계 9는 자동화하지 않는다 — 전문가 입력 파일이 준비되면 사용자가
  10을 요청한다.
- 여러 케이스 처리 시 단계 0~8을 케이스별로 반복하고, 10의 집계는 전체
  케이스 완료 후 1회 실행한다.

## 데이터 전달 프로토콜

- **파일 기반(주)**: 계약 JSON은 `outputs/CASE_XXX/`, 중간 텍스트는
  `data/processed/CASE_XXX/`. 에이전트 반환값에는 요약과 warnings만 담는다.
- **_workspace(보조)**: `_workspace/RUN_XXX/{순서}_{agent}_{artifact}.md`.
  오케스트레이터는 각 단계 후 노트의 warnings를 확인해 다음 에이전트
  프롬프트에 전달할 맥락을 뽑는다.
- 에이전트 호출 프롬프트에 반드시 포함: `case_id`, `run_id`, 담당 단계,
  이전 단계 warnings 요약, component-output-contract 스킬 준수 지시.

## 에러 핸들링

| 상황 | 대응 |
| --- | --- |
| 스키마 검증 FAIL | 해당 에이전트에 오류 메시지를 주고 1회 재시도. 재실패 시 **파이프라인 중단** + 사용자 보고 (부분 결과로 하류를 오염시키지 않는다 — 기본 "누락 명시 후 진행" 원칙의 의도적 예외) |
| 에이전트가 `status: "partial"` 반환 | warnings를 다음 단계에 전달하고 진행. 최종 보고에 partial 목록 명시 |
| OCR 실패율 과다 (문서 절반 이상) | 즉시 사용자 보고 — Go/No-Go의 No-Go 신호이므로 계속 돌리는 것이 낭비일 수 있다 |
| 정답지 접근 시도 감지 (critic 외) | 즉시 중단 + 보고. 해당 실행의 산출물은 평가에서 제외 |
| 상충 데이터 | 삭제하지 않고 출처 병기 (`inconsistencies`로 명시) |

## 완료 보고

실행 종료 시 사용자에게: 케이스별 산출물 경로, 검증 PASS/FAIL/SKIP 집계,
`review_required` 항목 수와 검수 라우팅(손사/의사), partial·경고 목록,
다음 행동(전문가 검수 대기 등). 실행 후 개선할 부분이 있는지 피드백을
요청한다 (하네스는 피드백으로 진화한다 — 루트 CLAUDE.md 변경 이력 참조).

## 테스트 시나리오

**정상 흐름**: `data/raw/CASE_001/`이 준비된 상태에서 "CASE_001 스크리닝
리포트까지 실행" → 단계 1→2a/2b→3→4→5 실행, 각 게이트 PASS,
`screening_report.json`이 §7 포함으로 검증 통과, 완료 보고에 검수 포인트
요약이 포함되어야 한다.

**에러 흐름**: claim-analysis가 `extracted_claim_fields.json`에 근거 없는
필드(`evidence_references` 누락)를 쓰면 → 게이트 FAIL → 오류 메시지와
함께 1회 재시도 → 수정본 PASS 시 진행, 재실패 시 4단계로 넘어가지 않고
중단 보고가 나와야 한다.
