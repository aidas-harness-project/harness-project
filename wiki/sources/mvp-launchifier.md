---
type: Source
title: From Idea to MVP (Launchifier Framework)
description: 아이디어 검증부터 출시 후 스케일/피벗 결정까지 MVP 개발 14단계를 정리한 Igor Royzis의 가이드.
resource: ../../sources/mvp-guide-royzis.md
url: https://www.linkedin.com/pulse/from-idea-mvp-your-no-nonsense-guide-building-what-really-igor-royzis-ke4ce/
author: Igor Royzis
published: 2024-10-31
tags: [source, mvp, 방법론, product]
timestamp: 2026-07-06T00:00:00+09:00
---

# 핵심 내용

MVP 개발을 14단계로 정리한 프레임워크("Launchifier"). 코드 작성 전
검증에 앞 단계 절반을 쓰는 것이 특징이다.

| 단계군 | 단계 | 요지 |
| --- | --- | --- |
| 검증 (1~3) | 아이디어 검증 → 시장 조사 → 비전·성공 기준 | 해결책이 아니라 **문제**를 먼저 정의. 사용자 직접 인터뷰(유도 질문 경계). 만들기 전에 수요 확인. 측정 가능한 KPI 설정 |
| 계획 (4~8) | 기능 우선순위 → 기술 타당성 → 팀 구성 → 프로토타입 → 로드맵 | 핵심 문제를 푸는 기능만. "이 기능이 없어도 핵심 문제가 풀리는가?" 질문으로 컷. 마일스톤 분할 + 버퍼 타임 |
| 실행 (9~12) | 개발 → 테스트/QA → 프리런치 마케팅 → 출시 | 반복적(iterative) 개발, 코드 리뷰 상시화, UAT, 성능 모니터링 |
| 학습 (13~14) | 피드백 분석 → 스케일/피벗 결정 | KPI 추적 → iterate / scale / pivot 중 선택 |

# PoC와의 연결

이 프레임워크에서 지금의 [Agent Harness PoC](../overview.md)는
**1~3단계(검증)에 해당**한다 — 제품이 아니라 "기존 모델 조합으로 손사
업무를 보조할 수 있는가"라는 가설의 검증이다. 대응 관계:

- **문제 정의** ↔ PoC의 [핵심 질문 5개](../overview.md).
- **사용자 인터뷰** ↔ [독립손해사정사 인터뷰](../references/case-qna.md).
- **성공 기준(KPI)** ↔ [평가 지표](../evaluation/metrics.md)의 정량 목표.
- **iterate/scale/pivot 결정** ↔ [Go/No-Go 기준](../evaluation/go-no-go.md)
  ("Go = scale 후보, No-Go = 범위 재설계" — 사실상 pivot 판단).
- **기능 우선순위 / Non-negotiable** ↔ PoC의 P0/P1 구분과
  [제외 범위](../overview.md).
- **마일스톤 + 버퍼** ↔ [3주 일정 매핑](../pipeline.md).

시사점: PoC가 Go로 판정되면 다음 단계는 이 프레임워크의 4~8단계
(MVP 계획 — 기능 컷, 기술 스택 확정, 프로토타입/UI, 로드맵)에 해당한다.
PoC 산출물 중 [7개 묶음 Agent](../pipeline.md)와
[표준 출력 계약](../templates/component-output.md)은 그대로 MVP의 기술
요구사항 문서의 뼈대가 될 수 있다.

# Citations

[1] [원문 추출본](../../sources/mvp-guide-royzis.md)
[2] [원문 (LinkedIn)](https://www.linkedin.com/pulse/from-idea-mvp-your-no-nonsense-guide-building-what-really-igor-royzis-ke4ce/)
