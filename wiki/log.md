# Wiki Update Log

엔트리 형식: `* **<op>** | <대상> — <설명>` (op: ingest / creation / update / answer / lint / deprecation).
최근 5개 보기: `grep -E "^\* \*\*" wiki/log.md | head -5`

## 2026-07-06

* **ingest** | 컴포넌트별 I/O 계약 초안 (GPT 정리) — `sources/pipeline_input-output.md` 검토 후 채택. [요약+평가 페이지](sources/pipeline-io-contracts.md) 생성, [표준 출력 계약](templates/component-output.md)을 실행 메타데이터(run_id·model_info 등) 포함으로 확장, [문서 유형](taxonomy/document-types.md)·[청구담보](taxonomy/claim-coverages.md)에 영어 표준 코드 열 추가, [스크리닝 리포트](templates/screening-report.md)에 JSON 병행 출력 규칙과 §7 누락 경고 추가. 갭 2건 기록(스크리닝 §7 1차 판단 누락, 케이스 집계 평가 리포트 부재).
* **ingest** | From Idea to MVP (Launchifier Framework) — LinkedIn 글(Igor Royzis) 추출본을 `sources/mvp-guide-royzis.md`에 보관, [요약 페이지](sources/mvp-launchifier.md) 생성. PoC를 MVP 검증 단계(1~3)로 위치 짓고 [개요](overview.md)·[Go/No-Go](evaluation/go-no-go.md)에 제품 관점 연결 추가.
* **ingest** | Phase별 파이프라인 초안 (GPT 정리) — `sources/pipeline_rough.md` 평가 후 채택. [파이프라인](pipeline.md)을 Phase 1/2 구조 + 7개 묶음 Agent + 주차 매핑으로 개편, [컴포넌트 표준 출력 계약](templates/component-output.md) 신설. 모순 2건 발견·해소(사건 유형 분류 누락 → ClaimAnalysis 묶음에 추가, 감액사유 추출 시점 → Week 2로 당김). 관련 갱신: [denial-reason](agents/denial-reason.md), [policy-mapping](agents/policy-mapping.md), [case-type](agents/case-type.md).
* **update** | 스키마 도입 — LLM Wiki 패턴 채택. [Wiki Schema](CLAUDE.md) 작성, git 버전 관리 시작, `sources/`·`answers/` 폴더 규약 신설, 루트 [index](index.md)를 전 페이지 카탈로그로 확장, log 형식을 파싱 가능한 엔트리 형식으로 전환.
* **ingest** | LLM Wiki 아이디어 파일 — 원문을 `sources/llm-wiki-idea-file.md`(raw)에 보관하고 [요약 페이지](sources/llm-wiki-idea.md) 생성.
* **update** | 링크 상대경로 전환 — Obsidian 호환을 위해 번들 내 모든 링크를 절대 경로(`/...`)에서 상대 경로로 변환. 케이스·참조 문서의 번들 외부 `resource`/인용 경로 깊이 수정.
* **creation** | OKF v0.1 번들 최초 생성 — [프로젝트 개요](overview.md), [파이프라인](pipeline.md), 에이전트 14종, 분류 체계, 템플릿, 평가 기준, 케이스 3건을 `POC guide.md`와 케이스 원자료로부터 정리.
