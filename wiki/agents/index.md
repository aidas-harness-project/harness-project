# 에이전트

파이프라인 순서대로 정렬. 전체 흐름은 [파이프라인 구조](../pipeline.md) 참고.

# 입력·전처리 (P0)

* [Document Intake Agent](document-intake.md) - 케이스 파일을 수집·정렬해 문서 목록 생성.
* [OCR / Text Extraction Layer](ocr-layer.md) - PDF·이미지에서 문서별 raw text 추출.
* [Redaction Agent](redaction.md) - 민감정보를 제거해 가명처리 텍스트 생성.
* [Document Classification Agent](document-classification.md) - 진단서/의무기록/약관/안내문 등 문서 유형 분류.
* [Field Extraction Agent](field-extraction.md) - 진단명, KCD, 사고일 등 핵심항목 추출.

# 스크리닝 (P0~P1)

* [Claim Coverage Agent](claim-coverage.md) - 청구담보(실손, 수술비, 후유장해 등) 식별.
* [Denial / Reduction Reason Agent](denial-reason.md) - 감액·부지급 사유 추출 및 분류.
* [Consistency Check Agent](consistency-check.md) - 문서 간 날짜·진단명·경위 불일치 탐지.
* [Case Type Classification Agent](case-type.md) - 사건 유형(후유장해/실손/수술비 등) 분류.

# 검토·초안 (P1)

* [Policy Mapping Agent](policy-mapping.md) - 관련 약관 조항 후보 탐색.
* [Rebuttal Point Agent](rebuttal.md) - 감액사유별 반박 논거 후보 생성.
* [Draft Writer Agent](draft-writer.md) - 손해사정서 초안 생성.
* [Evidence Check / Critic Agent](critic.md) - 근거 없는 주장·위험 표현 탐지.

# 평가

* [Evaluation Harness](evaluation-harness.md) - 모델 초안을 실제 최종 손사서와 비교.
