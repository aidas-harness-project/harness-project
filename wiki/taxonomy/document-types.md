---
type: Taxonomy
title: 문서 유형 분류
description: 케이스 입력 문서의 유형 — 진단서, 의무기록, 약관, 보험사 안내문 등.
tags: [taxonomy, 문서유형]
timestamp: 2026-07-06T00:00:00+09:00
---

[Document Classification Agent](../agents/document-classification.md)가 쓰는
문서 유형 분류.

# 유형

| 유형 | 설명 | 주요 추출 항목 |
| --- | --- | --- |
| 보험증권 | 가입 담보·가입금액 확인 | 담보명, 가입금액 |
| 약관 | 지급요건·면책 조항의 근거 | 조항 텍스트 ([Policy Mapping](../agents/policy-mapping.md) 입력) |
| 진단서 | 진단명·치료 필요성 | 진단명, KCD, 발병일 |
| 의무기록 | 치료 경과·통증 기록 | 치료기간, 경과 기록 |
| 영상판독지 | 영상의학적 소견 | 판독 소견 |
| 영수증 | 치료비·치료일자 | 금액, 치료일자 |
| 보험사 안내문 | 부지급·감액 통보 | 감액사유 문구 ([Denial Reason](../agents/denial-reason.md) 입력) |
| 손해사정서 | 정답지(모델에 숨김) 또는 참고 | 평가 전용 |

# Citations

[1] [PoC 가이드](../references/poc-guide.md)
