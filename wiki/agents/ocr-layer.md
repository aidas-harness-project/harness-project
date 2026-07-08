---
type: Agent
title: OCR / Text Extraction Layer
description: PDF·이미지에서 페이지 단위 텍스트를 추출하고, 품질 로그와 불명확 영역(uncertain_regions)을 남기는 계층.
tags: [agent, p0, ocr]
priority: P0
pipeline_order: 2
timestamp: 2026-07-07T00:00:00+09:00
---

# 역할

PDF 내장 텍스트 추출과 이미지 OCR을 연결해 문서별 raw text를 만든다.

# 요구사항

- 페이지 단위로 텍스트 저장 (`data/processed/CASE_XXX/DOC_XXX/page_NNN.md`).
- 문서별 OCR 품질 로그 생성 → `ocr_result.json`
  (`schemas/ocr_result.schema.json` v0.1).
- **불명확 영역 기록**: confidence가 임계값 미만인 블록을
  `uncertain_regions`로 기록한다 — 읽어낸 텍스트 그대로(`text_as_read`),
  confidence, 페이지 텍스트 파일 내 **문자 오프셋**(겹침 판정용).
  임계값은 엔진마다 분포가 달라 고정하지 않고, 실행 시 사용한 값을
  `uncertain_confidence_threshold`로 결과에 기록한다.
- manifest에는 문서별 요약만 복사(owner: OCR): `ocr_quality`
  (high/medium/low), `uncertain_region_count`. 상세 목록은
  `ocr_result.json`에만 둔다 — manifest는 출석부로 가볍게 유지.
- OCR 실패 문서 표시 — 실패율이 높으면 [Go/No-Go 기준](../evaluation/go-no-go.md)의
  No-Go 조건("OCR 품질 때문에 문서 이해가 거의 불가능하다")에 해당할 수 있다.

# 불명확 영역 전파 규칙

뒤 단계가 원문을 인용(`evidence_references.quote`)할 때 인용 구간이
`uncertain_region`과 겹치면 해당 필드는 `review_required: true`로
라우팅한다 (PoC에서는 이 단순 규칙으로 시작 — confidence 수치 감쇠는
평가 데이터가 쌓인 뒤 검토).

- [Field Extraction](field-extraction.md): 불명확 영역에서 나온 추출값은
  검수 대상.
- [Consistency Check](consistency-check.md): 문서 간 불일치 보고 시 진짜
  불일치인지 OCR 오독인지 구분하는 단서로 사용.

# 참고 — 정확도 기대치

한국어(90–95%) + 손글씨 혼재 의무기록(75–85%) + 표 많은 약관(80–95%)
조합은 업계 기준표에서 가장 낮은 정확도 구간이 겹친다. 모델별·영역별
기준치와 2026년 벤치마크는
[OCR 정확도 벤치마크](../answers/ocr-accuracy-benchmarks.md) 참고 —
최신 특화 모델로도 표·저품질 스캔은 90% 초반대라 `uncertain_regions`
계약은 유효하다.

# 주의

케이스 원자료 중 일부 텍스트 파일은 CP949(EUC-KR) 인코딩이다. UTF-8로
가정하고 읽으면 깨지므로 인코딩 감지가 필요하다 — 감지 결과는
`encoding_detected`로 기록한다.

# 다음 단계

[Redaction Agent](redaction.md).

# Citations

[1] [PoC 가이드](../references/poc-guide.md)
[2] [컴포넌트별 I/O 계약 초안 (GPT 정리)](../sources/pipeline-io-contracts.md)
