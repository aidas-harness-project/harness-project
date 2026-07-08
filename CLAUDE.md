# 손해사정 Agent Harness PoC + LLM Wiki (Second Brain)

이 vault는 두 가지를 담는다:

1. **손해사정 Agent Harness PoC** — 가명처리된 종결 케이스로 스크리닝
   리포트·손해사정서 초안 생성을 검증하는 3주 실험. 기획 문서는
   `POC guide.md`, 케이스 원자료는 `POC/`.
2. **LLM Wiki** (`wiki/`) — LLM이 작성·유지보수하는 사용자의 second brain.
   PoC 지식에서 시작했지만 모든 주제로 확장된다.

## 반드시 지킬 것

- **모든 세션은 `wiki/CLAUDE.md`의 스키마를 따른다.** 위키 구조, 폴더
  규약, ingest/query/lint 워크플로, index/log 형식이 모두 거기 정의되어
  있다. 위키를 읽거나 쓰기 전에 해당 스키마를 먼저 확인할 것.
- **Raw sources는 불변**: `POC/`, `case_qna.pdf`, `sources/`는 읽기 전용.
  수정·삭제 금지.
- 프로젝트 파일을 만들거나 바꾼 뒤, 그 변경이 위키가 다루는 지식에
  해당하면 위키를 함께 갱신한다 (스키마의 "갱신 시 공통 체크리스트" 참고).
- `POC/` 안의 최종 손해사정서는 평가용 정답지다. 모델 입력으로 쓰지 말 것.
- 한국어 텍스트 파일 중 CP949 인코딩이 있으니 읽을 때 주의.

## 도구

- 위키 무결성 검사: `python tools/wiki_lint.py`
- 산출물 스키마 검증: `python tools/validate_output.py <파일.json>`
- git으로 버전 관리한다. 커밋은 사용자가 요청할 때 또는 의미 있는 위키
  변경 단위가 완료됐을 때 제안한다.

## 하네스: 손해사정 케이스 파이프라인

**목표:** 종결 케이스 입력 → 스크리닝 리포트 + 손사서 초안 + 평가를
7개 전문 에이전트로 자동 생성한다.

**트리거:** 케이스 처리·파이프라인 실행·재실행·평가 요청 시
`loss-adjustment-pipeline` 스킬을 사용하라. 파이프라인 설계에 대한 단순
질문은 `wiki/pipeline.md`로 직접 응답 가능.

**변경 이력:**
| 날짜 | 변경 내용 | 대상 | 사유 |
|------|----------|------|------|
| 2026-07-07 | 초기 구성 (에이전트 7 + 스킬 2 + 도구 2) | 전체 | - |
| 2026-07-08 | 노트 먼저·증분 작성 규율 추가 | skills/component-output-contract | 관찰 1: claim-analysis가 노트 없이 종료(중단 유실) |
| 2026-07-08 | 중단 이어받기(resume) + 노트 존재 검증 경로 | skills/loss-adjustment-pipeline | 관찰 2: 파일 스티칭 복원력을 의도된 기능으로 승격 |
| 2026-07-08 | primary 진단코드 선택 규칙 (문서 성격 기반 우선순위) | agents/claim-analysis + wiki/field-extraction | 관찰 3: 후유장해 케이스에서 headline KCD 오선택(F1 실패유형) |
