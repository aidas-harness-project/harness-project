---
type: Template
title: 스크리닝 리포트 템플릿
description: 핵심항목·청구담보·감액사유·불일치를 통합한 1차 스크리닝 리포트의 7개 섹션 구조.
tags: [template, screening]
timestamp: 2026-07-07T00:00:00+09:00
---

파이프라인 1~9단계 결과를 통합해 자동 생성한다. 부족서류 후보와
손사·의사 검수 필요 포인트를 반드시 표시한다.

# 구조

```markdown
# 스크리닝 리포트

## 1. 사건 개요
- 사건 ID:
- 사건 유형:
- 주요 진단명:
- 사고일 / 발병일:
- 치료기간:
- 주요 청구담보:

## 2. 보험사 판단
- 부지급/감액 여부:
- 감액사유:
- 보험사 주장 요약:
- 감액금액 / 지급제외금액:

## 3. 핵심 쟁점
- 쟁점 1:
- 쟁점 2:
- 쟁점 3:

## 4. 문서 간 불일치
- 날짜 불일치:
- 진단명 불일치:
- 사고경위 불일치:
- 치료기간 불일치:

## 5. 추가 필요 서류
- 필요 서류:
- 요청 사유:

## 6. 전문가 검수 포인트
- 손사 검수 필요:
- 의사 검수 필요:

## 7. 1차 판단
- 진행 가능성:
- 난이도:
- 우선 검토 포인트:
```

# 출력 형식

`screening_report.json`(machine-readable)과 `screening_report.md`(사람용)를
함께 생성한다 — 형식은 [I/O 계약](../sources/pipeline-io-contracts.md) 참고.

> ⚠️ I/O 계약 초안의 예시에는 **§7 "1차 판단"이 빠져 있다.** JSON에
> `preliminary_assessment`(진행 가능성·난이도·우선 검토 포인트) 블록을
> 추가해서 이 템플릿의 7개 섹션을 모두 채울 것 — 블록 정의와 처리
> 시점은 [해결 계획](../answers/pipeline-understanding-and-gap-plan.md) 참고.

# 섹션별 데이터 출처

| 섹션 | 생성 주체 |
| --- | --- |
| 1. 사건 개요 | [Field Extraction](../agents/field-extraction.md), [Claim Coverage](../agents/claim-coverage.md), [Case Type](../agents/case-type.md) |
| 2. 보험사 판단 | [Denial Reason](../agents/denial-reason.md) |
| 4. 문서 간 불일치 | [Consistency Check](../agents/consistency-check.md) |

# Citations

[1] [PoC 가이드](../references/poc-guide.md)
