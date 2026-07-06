---
type: Template
title: 반박 포인트 리포트 형식
description: 감액사유별 보험사 주장·반박 후보·근거 자료·검수 필요를 정리하는 출력 형식.
tags: [template, rebuttal]
timestamp: 2026-07-06T00:00:00+09:00
---

[Rebuttal Point Agent](../agents/rebuttal.md)의 케이스별 출력 형식.

# Examples

```markdown
# 반박 포인트

## 감액사유
- 치료 필요성 부족

## 보험사 주장
- 도수치료 횟수가 과도하고 의학적 필요성이 부족하다는 취지

## 반박 후보
1. 의무기록상 통증 지속 및 기능 제한 기록이 확인됨
2. 진단서상 보존적 치료 필요성이 기재되어 있음
3. 치료기간과 증상 경과가 단절 없이 이어짐

## 근거 자료
- 의무기록 p.4: 통증 지속 기록
- 진단서 p.1: 진단명 및 치료 필요성
- 영수증 p.2: 치료일자

## 검수 필요
- 치료 횟수의 적정성은 정형외과 전문의 검수 필요
```

# 규칙

- 반박 후보마다 근거 자료(문서·페이지)를 연결한다.
- 근거를 연결할 수 없는 반박은 반드시 "검수 필요"로 표시한다.
- 감액사유 코드는 [감액사유 Taxonomy](../taxonomy/reduction-reasons.md)를 따른다.

# Citations

[1] [PoC 가이드](../references/poc-guide.md)
