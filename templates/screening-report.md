---
type: Template
title: 스크리닝 리포트 템플릿
description: 핵심항목·청구담보·감액사유·불일치를 통합한 1차 스크리닝 리포트의 7개 섹션 구조.
tags: [template, screening]
timestamp: 2026-07-13T00:00:00+09:00
adopted_from: wiki/templates/screening-report.md
---

> **provenance**: `wiki/templates/screening-report.md`에서 2026-07-13에
> harness-project로 채택한 사본. 이 파일이 1차 소스 — [draft-report.md](draft-report.md)
> 참고.

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

`screening_report.json`(machine-readable, `schemas/screening_report.schema.json`
준수)과 `screening_report.md`(사람용, `tools/document_assembly.py`로 렌더)를
함께 생성한다.

# 섹션별 데이터 출처

| 섹션 | 생성 주체 |
| --- | --- |
| 1. 사건 개요 | [claim-analysis agent](../.claude/agents/claim-analysis.md) |
| 2. 보험사 판단 | [denial-response agent](../.claude/agents/denial-response.md) |
| 4. 문서 간 불일치 | [consistency-check agent](../.claude/agents/consistency-check.md) |

# Citations

[1] [PoC 가이드](../POC%20guide.md)
