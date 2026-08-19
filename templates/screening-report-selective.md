---
type: Template
title: 스크리닝 리포트 템플릿 (선택적 레인)
description: source-grounded selective 레인의 9개 보고 영역 구조. 가능성·난이도·지급 가능성 판단을 포함하지 않는다.
tags: [template, screening, selective]
timestamp: 2026-08-20T00:00:00+09:00
---

> **provenance**: Notion 기준문서 「Claim Analysis · Consistency Check ·
> Screening Report 재구축 계획」(Accepted, 2026-08-19) §7의 9개 보고 영역.
> 기존 [screening-report.md](screening-report.md)는 legacy 레인용으로 그대로 두고,
> 이 파일은 selective 레인 전용이다.

# 기존 템플릿과 다른 점

legacy 템플릿의 7번 「1차 판단」(진행 가능성·난이도)을 **두지 않는다.** 그 판단은
자료로 뒷받침되지 않는 전망이며, 실무 보고서에서 근거 있는 사실과 나란히 놓이면
같은 무게로 읽힌다. 대신 「미확인 항목」을 두어 무엇이 확인되지 않았는지를 남긴다.

함께 제외하는 것: 청구 권고, 일반적인 전문가 검토 권고, 예상 보험금·손해액,
최종 지급 가능성, Draft Report용 강조사항, 모델이 새로 만든 포괄적 우선순위,
개발 trace·provider·token·stop reason.

# 구조

```markdown
# 스크리닝 리포트

## 1. 사고와 공통 의료정보
- 사고일:
- 사고 경위:
- 손상 부위 / 좌우:
- 주요 진단명:
- 치료기간:

## 2. 사건유형 병렬 판정
- 개인보험:
- 교통사고:
- 산재·근재:
- 배상:

## 3. 유형별 추가정보와 접수 상태
- 접수 여부:
- 접수 근거:

## 4. 의료영역 확보 현황
- 영역별 A/B 확보:

## 5. 필요서류 체크리스트
- 보유:
- 미확인:

## 6. 중요 충돌과 유형 판정 영향
- 확인된 불일치:
- 유형 판정에 대한 영향:

## 7. 주요 미확인 항목
- 항목과 사유:

## 8. 관련 약관과 요건 자료상태
- 조항:
- 요건 자료상태:

## 9. 보험사 응답
- 거절:
- 감액:
- 승인:
```

# 출력 형식

`screening_report.json`(`schemas/screening_report_selective.schema.json` 준수)과
`screening_report.md`(`tools/document_assembly.py --template screening_report_selective`로
렌더). `[E#]` 태그와 evidence sidecar는 그 도구가 생성하며 에이전트가 직접 쓰지 않는다.

# 섹션별 생성 주체

| 섹션 | 생성 주체 |
| --- | --- |
| 1~5, 7, 8 | helper의 결정론적 조립 (claim_analysis_result) |
| 6 | conflict ledger의 `professional_summary`를 그대로 복사 |
| 9 | denial_reason_result (있을 때만) |
| 중요도·배치·검토 포인트 | screening-report agent |
