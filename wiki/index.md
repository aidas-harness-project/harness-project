---
okf_version: "0.1"
---

# 손해사정 Agent Harness PoC — LLM Wiki

가명처리된 독립손해사정사 종결 케이스를 입력으로, 스크리닝 리포트와
손해사정서 초안을 생성하는 Agent Harness PoC의 지식 번들이자 사용자의
second brain. 운영 규칙은 [Wiki Schema](CLAUDE.md) 참고.

# 개요

* [프로젝트 개요](overview.md) - PoC 정의, 핵심 질문, 포함/제외 범위.
* [파이프라인 구조](pipeline.md) - Phase 1(최초 청구/검토)과 Phase 2(반려·감액 대응)로 나뉜 파이프라인 구조와 구현용 7개 묶음 Agent, 3주 일정 매핑.

# 에이전트 (agents/)

* [Document Intake Agent](agents/document-intake.md) - 케이스 폴더의 파일을 수집·정렬해 문서 목록을 생성하는 파이프라인 진입점.
* [OCR / Text Extraction Layer](agents/ocr-layer.md) - PDF·이미지에서 페이지 단위 텍스트를 추출하고 OCR 품질 로그를 남기는 계층.
* [Redaction Agent](agents/redaction.md) - OCR 텍스트에서 민감정보를 제거해 가명처리 텍스트를 만드는 에이전트.
* [Document Classification Agent](agents/document-classification.md) - 문서를 진단서·의무기록·약관·안내문 등 유형으로 분류하고 confidence를 저장.
* [Field Extraction Agent](agents/field-extraction.md) - 진단명, KCD, 사고일, 치료기간, 수술명, 병원명 등 핵심항목을 구조화 JSON으로 추출.
* [Claim Coverage Agent](agents/claim-coverage.md) - 보험증권·약관·청구정보에서 청구담보를 식별하고 근거 문장과 confidence를 저장.
* [Denial / Reduction Reason Agent](agents/denial-reason.md) - 보험사 안내문에서 감액·부지급 문구를 추출하고 taxonomy 코드로 분류.
* [Consistency Check Agent](agents/consistency-check.md) - 문서 간 날짜·진단명·사고경위·치료기간 불일치를 탐지하고 심각도를 점수화.
* [Case Type Classification Agent](agents/case-type.md) - 케이스를 후유장해·진단/수술비·실손·배상책임 등 사건 유형으로 분류.
* [Policy Mapping Agent](agents/policy-mapping.md) - 담보명·감액사유 기반으로 관련 약관 조항 후보 리스트를 검색해 제시.
* [Rebuttal Point Agent](agents/rebuttal.md) - 감액사유별 반박 프레임에 따라 약관·의무기록 근거를 연결한 반박 논거 후보를 생성.
* [Draft Writer Agent](agents/draft-writer.md) - 스크리닝 결과와 반박 포인트를 사건 유형별 손사서 목차에 채워 초안을 생성.
* [Evidence Check / Critic Agent](agents/critic.md) - 초안 문장별 근거를 연결하고 근거 없는 주장·과도한 법률/의료 표현에 검수 필요 태그를 부여.
* [Evaluation Harness](agents/evaluation-harness.md) - 모델 산출물을 실제 최종 손사서·지급 결과와 항목별로 비교해 평가 리포트를 생성.

# 분류 체계 (taxonomy/)

* [감액사유 Taxonomy v1](taxonomy/reduction-reasons.md) - 보험사 감액·부지급 사유를 R01~R99 코드로 표준화한 분류 체계.
* [사건 유형 분류](taxonomy/case-types.md) - 케이스 단위 분류 — 후유장해, 진단·수술비, 실손, 배상책임, 기타.
* [청구담보 분류](taxonomy/claim-coverages.md) - 청구담보 표준화 목록 — 실손의료비, 수술비, 진단비, 상해후유장해 등.
* [문서 유형 분류](taxonomy/document-types.md) - 케이스 입력 문서의 유형 — 진단서, 의무기록, 약관, 보험사 안내문 등.

# 템플릿 (templates/)

* [스크리닝 리포트 템플릿](templates/screening-report.md) - 핵심항목·청구담보·감액사유·불일치를 통합한 1차 스크리닝 리포트의 7개 섹션 구조.
* [손해사정서 초안 기본 구조](templates/draft-report.md) - 개요·쟁점·약관·의학·감액 검토·의견으로 이어지는 손사서 초안 8개 섹션 구조.
* [반박 포인트 리포트 형식](templates/rebuttal-points.md) - 감액사유별 보험사 주장·반박 후보·근거 자료·검수 필요를 정리하는 출력 형식.
* [금지 표현 가이드](templates/forbidden-expressions.md) - 손사서 초안에서 피해야 할 단정적 법률·의료 표현과 그 대체 표현.
* [컴포넌트 표준 출력 계약](templates/component-output.md) - 파이프라인의 모든 컴포넌트 출력이 공통으로 포함해야 하는 필드 — confidence, 근거 참조, 검수 플래그, 환각·금지표현 체크.

# 평가 (evaluation/)

* [평가 지표](evaluation/metrics.md) - 3주 PoC의 정량 목표치(정확도·일치율·처리시간)와 전문가 정성 평가 기준.
* [Go / No-Go 기준](evaluation/go-no-go.md) - 3주 PoC 종료 시 다음 단계 진행 여부를 판정하는 조건 목록.

# 케이스 (cases/)

* [골다공증 기여도 감액 케이스](cases/preexisting-condition.md) - 척추 장해지급률 50% 해당 사례에서 골다공증 기여도 10%p 공제로 40%가 인정된 기왕증 감액 케이스.
* [뇌혈관질환진단비 분쟁 케이스](cases/coverage-dispute.md) - 뇌혈관질환진단비의 약관상 지급범위를 두고 KB·농협·삼성화재·한화 4개 보험사와 다툰 케이스.
* [상완골 골절 후유장해 케이스](cases/permanent-disability.md) - 상완골 근위부 골절 수술(OP) 후 배상책임 손해사정으로 종결된 후유장해 케이스.

# 소스 (sources/)

* [LLM Wiki 패턴 아이디어 파일](sources/llm-wiki-idea.md) - LLM이 유지보수하는 개인 지식 베이스 패턴 — 이 위키 운영 방식의 근거 문서.
* [Phase별 파이프라인 초안 (GPT 정리)](sources/pipeline-rough-gpt.md) - 파이프라인을 Phase 1(최초 청구/검토)과 Phase 2(반려·감액 대응)로 분리하고 구현용 7개 묶음 Agent를 제안한 설계 초안.
* [From Idea to MVP (Launchifier Framework)](sources/mvp-launchifier.md) - 아이디어 검증부터 출시 후 스케일/피벗 결정까지 MVP 개발 14단계를 정리한 Igor Royzis의 가이드.

# 참고 자료 (references/)

* [PoC 가이드](references/poc-guide.md) - 스크리닝→약관 매핑→반박 포인트→손사서 초안 Agent Harness PoC의 기획·일정·평가 문서.
* [케이스별 업무 흐름 인터뷰 메모](references/case-qna.md) - 실제 독립손해사정사 인터뷰를 정리한 케이스별 업무 흐름 메모 PDF.
