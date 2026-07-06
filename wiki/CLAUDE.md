---
type: Schema
title: Wiki Schema — LLM Wiki 운영 규칙
description: 이 위키를 유지보수하는 LLM이 따라야 할 구조·규약·워크플로 정의.
timestamp: 2026-07-06T00:00:00+09:00
---

# Wiki Schema — LLM Wiki 운영 규칙

이 문서는 `wiki/`의 **스키마**다. 이 vault에서 작업하는 모든 LLM 세션은
아래 규칙을 따라 위키를 유지보수한다. 패턴의 배경 아이디어는
`sources/llm-wiki-idea-file.md`(원문), 요약은
`wiki/sources/llm-wiki-idea.md` 참고. 이 스키마는 사용자와 함께
운영하면서 계속 개선한다 — 규칙이 실무와 어긋나면 사용자와 논의해
스키마를 고친다.

## 3계층 구조

| 계층 | 위치 | 소유권 |
| --- | --- | --- |
| Raw sources | `POC/`, `case_qna.pdf`, `sources/` | **불변**. LLM은 읽기만 한다. 수정·삭제 절대 금지 |
| Wiki | `wiki/` | LLM이 전적으로 작성·유지보수. 사용자는 읽는다 |
| Schema | 이 파일 + 루트 `CLAUDE.md` | 사용자와 LLM이 함께 발전시킨다 |

새 원자료는 `sources/`에 넣는다 (웹 클리핑, 논문, 메모 등).
케이스 원자료는 기존대로 `POC/` 하위에 케이스 폴더 단위로 넣는다.

## 문서 포맷 (OKF v0.1 준수)

- 모든 개념 문서는 YAML frontmatter로 시작하며 **`type` 필수** (비어 있으면 안 됨).
- 권장 필드: `title`(한국어 표시명), `description`(한 줄 요약 — index 생성에 쓰임),
  `tags`, `timestamp`(ISO 8601, KST), 원자료가 있으면 `resource`(상대 경로).
- 파일명은 영문 kebab-case. 예약 파일명 `index.md`, `log.md`는 개념 문서로 쓰지 않는다.
- `index.md`/`log.md`에는 frontmatter를 넣지 않는다
  (예외: 번들 루트 `wiki/index.md`의 `okf_version`만 허용).
- 링크는 **상대 경로만** 사용 (Obsidian 호환). 번들 절대 경로(`/...`) 금지.
- 깨진 링크는 오류가 아니다 — 아직 안 쓴 페이지를 가리키는 표시로 허용된다.
  단, lint 시 목록화해서 채울지 판단한다.

## 폴더 규약

| 디렉터리 | 내용 | 대표 type |
| --- | --- | --- |
| `wiki/` 루트 | 프로젝트 개요, 파이프라인 등 최상위 개념 | Project, Architecture |
| `agents/` | 파이프라인 에이전트 (1개념 = 1에이전트) | Agent |
| `taxonomy/` | 분류 체계 (감액사유, 사건유형, 담보, 문서유형) | Taxonomy |
| `templates/` | 산출물 템플릿·작성 가이드 | Template, Reference |
| `evaluation/` | 평가 지표·판정 기준 | Metric, Decision Criteria |
| `cases/` | 케이스 개념 (1개념 = 1케이스, `resource`로 POC/ 연결) | Case |
| `sources/` | **ingest된 원자료 1건당 요약 페이지 1개** (`resource`로 원본 연결) | Source |
| `answers/` | 보존 가치 있는 질의 답변·분석 (frontmatter에 `question` 필드) | Answer |
| `references/` | 번들 밖 자료를 가리키는 포인터 | Reference |

새 주제 영역이 생기면 새 하위 디렉터리를 만들고 `index.md`를 함께 만든다.
이 위키는 PoC 전용이 아니라 사용자의 **second brain**이다 — 프로젝트와
무관한 주제(학업, 리서치, 개인 관심사)도 같은 규칙으로 새 디렉터리에 축적한다.

## Operations

### Ingest — 새 원자료가 들어왔을 때

1. 원자료를 raw 계층(`sources/` 또는 `POC/`)에 둔다. 이미 있으면 그대로 읽는다.
   (한국어 텍스트 파일은 CP949 인코딩일 수 있음 — 깨지면 인코딩 감지)
2. 핵심 내용을 사용자와 짧게 논의하거나 요약을 제시한다.
3. `wiki/sources/`에 요약 페이지를 만든다 (`type: Source`, `resource`로 원본 연결).
4. **기존 개념 페이지들을 갱신한다** — 새 정보 반영, 상호 링크 추가.
   기존 서술과 모순되면 덮어쓰지 말고 본문에 모순을 명시적으로 기록한다.
   예: `> ⚠️ 모순: 소스 A는 X라고 하나 소스 B는 Y라고 함` (각 소스 페이지로 링크).
5. 영향받은 디렉터리의 `index.md`를 갱신한다 (루트 index 포함).
6. `log.md`에 ingest 엔트리를 추가한다.

### Query — 질문에 답할 때

1. `wiki/index.md`를 먼저 읽고 관련 페이지를 찾은 뒤 드릴다운한다.
   (원자료 재탐색은 위키에 답이 없을 때만)
2. 답변에는 근거가 된 위키 페이지를 인용한다.
3. 보존 가치 있는 답변(비교 분석, 새로운 연결, 종합)은 `answers/`에
   파일링을 제안하고, 사용자가 동의하면 저장 후 관련 페이지에서 링크한다.

### Lint — 주기적 건강 점검 (사용자가 "린트해줘" 요청 시)

1. `python tools/wiki_lint.py` 실행 — frontmatter/type 누락, 깨진 링크,
   고아 페이지(들어오는 링크 0개)를 기계적으로 검출.
2. 내용 점검: 페이지 간 모순, 새 소스가 갱신했어야 할 낡은 주장,
   언급만 되고 페이지가 없는 개념, 빠진 상호 링크, 웹 검색으로 채울 수
   있는 공백.
3. 발견 사항을 고치고, 조사할 만한 새 질문·찾아볼 소스를 제안한다.
4. `log.md`에 lint 엔트리를 추가한다.

## 갱신 시 공통 체크리스트

1. 수정한 개념 문서의 `timestamp` 갱신.
2. 문서 추가/삭제 시 해당 디렉터리와 루트의 `index.md` 반영.
3. `log.md` 엔트리 추가 (아래 형식).
4. 하나의 변경이 여러 페이지에 영향을 주면 **한 번에 모두** 갱신한다 —
   상호 참조 동기화가 이 패턴의 핵심 가치다.

## index.md 규약

- **루트 `wiki/index.md`**: 위키 전체 카탈로그. 모든 페이지를 카테고리별로
  나열하고, 각 항목은 `* [제목](경로) - 한 줄 설명` 형식 (설명은 해당
  페이지 frontmatter의 `description`과 일치시킨다).
- **디렉터리별 `index.md`**: 해당 디렉터리 페이지만 나열. 형식 동일.
- 질의 응답 시 LLM은 항상 루트 index부터 읽는다. 위키가 커져서 index만으로
  부족해지면 검색 도구 도입을 사용자와 논의한다.

## log.md 규약

- 날짜 헤딩(`## YYYY-MM-DD`, 최신이 위) 아래에 엔트리를 쌓는다.
- 엔트리 형식: `* **<op>** | <대상> — <설명>` (같은 날짜 안에서도 최신이 위)
- `<op>`은 소문자: `ingest` `creation` `update` `answer` `lint` `deprecation`
- 파싱: `grep -E "^\* \*\*" wiki/log.md | head -5` → 최근 5개 엔트리.
