---
type: Reference
title: 스크리닝 리포트 정답지 대조 루브릭
description: 스크리닝 리포트가 실제 손해사정사 문서와 얼마나 부합하는지를 4개 차원으로 채점하는 PoC 기준.
tags: [rubric, screening, fidelity, evaluation]
timestamp: 2026-08-25T00:00:00+09:00
rubric_version: screening_fidelity.v0.1
---

> **PoC 단계의 기준 루브릭.** 리포트가 잘 쓰였는지를 보는 참조 없는 루브릭
> (`rubric-screening-report.md`)과 달리, 이 루브릭은 **실제 손해사정사가 작성한 문서와
> 얼마나 맞는지**를 본다. 두 점수는 합치지 않는다.

# 1. 무엇을 재는가

인테이크가 `ground_truth`로 분류한 문서 — 해당 케이스의 실제 손해사정 결과물 — 를 정답지로 두고,
파이프라인이 만든 스크리닝 리포트가 그 정답지와 얼마나 부합하는지를 0–100으로 기록한다.

손해사정사가 놓친 것을 리포트가 잡았을 가능성은 배제하지 않는다. `손사_미기재`로 분류해 남기되,
**점수에는 반영하지 않는다.** PoC 단계의 질문은 "정답지와 맞는가"이지 "정답지보다 나은가"가 아니다.

# 2. 접근 권한과 유출 방지

채점자는 `harness-guardrails-dev` D1이 예외로 지정한 **유일한 스테이지**다.

- 정답지는 `python tools/dao.py read-ground-truth CASE_ID --caller-stage screening_fidelity
  --version screening [--list | --file NAME]`으로만 읽는다. 직접 파일 열기는 `.claude/settings.json`의
  deny 글롭이 채점자에게도 그대로 적용된다.
- **리포트의 런이 끝난 케이스에서만** 읽힌다 — 리포트가 존재하고 `screening_report`가 `passed`일 것.
  **사람이 읽었는지는 묻지 않는다.** 이 채점은 파이프라인 성능을 재는 것이고, 누가 리포트를
  읽었는지는 그 측정과 무관하다. 대신 막는 것은 **자기 런이 실패한 리포트를 채점하는 일**이다
  (CASE_712: document_processing 진행 중, 이후 4개 스테이지 failed, 그런데 리포트 파일은 존재).
  초안 경로(`evaluation`)는 사람 사인오프 게이트를 그대로 유지하며, 두 토큰은 서로 통용되지 않는다.
- **채점 결과는 종단이다.** 규칙이 아니라 구조로 그렇다 — 결과는
  `data/ground_truth/CASE_ID/_verification/screening_fidelity_result_v{n}.json`에 놓이고,
  그 자리는 deny 글롭이 이미 걸린 트리다. 쓰기·읽기 모두 `screening_fidelity` 스테이지로 제한된
  `dao.py write-verification-result` / `read-verification-result`로만 가능하다.
  `outputs/`에 두면 `read-contract`가 스테이지를 묻지 않으므로 모든 생산 스테이지가 읽는다.

## 결과 파일에 정답지 원문을 남기지 않는다

채점 결과가 생산 스테이지가 읽을 수 있는 자리에 놓이면, 결과 파일 자체가 정답지 유출 경로가 된다.
그래서 계약에 **정답지 원문 인용 필드가 존재하지 않는다.**

| 어느 쪽 | 결과에 남길 수 있는 것 |
| --- | --- |
| 스크리닝 리포트 | 원문 인용 (verbatim). 채점 근거로 요구된다 |
| 정답지 | **위치 참조**(문서명 + 페이지/항목)와 **짧은 필드값**(날짜·코드·금액·진단명 등, 200자 이내). 서술 문장은 남기지 않는다 |

`rationale`·`description`에도 정답지 문장을 옮겨 적지 않는다. "정답지 A면의 진단명과 불일치"라고 쓰지,
정답지 문장을 인용해 설명하지 않는다.

# 3. 네 개 차원

| | 재는 것 | 가중 | 헤드라인 |
| --- | --- | --- | --- |
| **F1** 사실 항목 일치 | 양쪽에 다 존재할 수 있는 값을 필드 단위로 대조 | 50 | 예 (fact만) |
| **F2** 쟁점 예측 리콜 | 정답지가 다룬 쟁점 중 리포트가 미리 짚은 비율 | 30 | 예 |
| **F3** 필요서류 일치 | 정답지가 근거로 삼은 문서를 리포트가 맞게 분류했는가 | 20 | 예 |
| **F4** 결론 방향 일치 | 1차 판단이 최종 결론 방향과 맞는가 | — | **아니오** |

---

## F1 · 사실 항목 일치 — 가중 50

**항목 집합은 채점자가 고르지 않는다.** `config/claim_analysis/claim_analysis_routing_v0.1.json`의
`fields`가 분모다(64개, `behavior_enabled: true`, 승인 2026-08-20 pyun). 대조는 `field_id` 단위로 하고,
결과에도 `field_id`를 기록한다.

### 등급 — 축은 둘이고 필드마다 정확히 하나다

| 도메인 종류 | 등급 필드 | 값 |
| --- | --- | --- |
| `medical` (8개 도메인) | `medical_advisory_grade` | A / B / C / D |
| `legal_factual` (`liability_basis`) | `priority_grade` | A / B / C |

스키마가 `oneOf`로 강제한다 — *"exactly one grading axis … never both and never neither"*.
과실비율을 의사 기준으로 매기는 것이 무의미했기 때문이다(그렇게 했을 때 배상책임 판단의 유일한
근거 필드가 의학적으로 안 중요하다는 이유로 C에 앉았다). 채점자는 두 축을 합치지 말고
`selection.field_grade()`와 같은 규칙으로 읽는다: 의료 등급이 있으면 그것, 없으면 `priority_grade`.

**따라서 루브릭이 예전에 손으로 적었던 A등급 목록은 폐기한다.** A는 두 축의 A를 합친 집합이다 —
의료 A 9개 + 법률 A 3개(`legal_basis_cited`, `liability_opinion_conclusion`,
`comparative_negligence_rate`). 과실비율과 적용 법조는 **재량이 아니라 A등급 추출 대상**이다.

### 핵심 필드도 이미 정의돼 있다

`critical_conflict_field: true`인 필드(32개)가 그것이다 — *"fields whose disagreement can change a
determination"* (`run_consistency_check.critical_field_ids`). 채점자가 고르는 목록이 아니라
설정에서 읽는다. `core_field_mismatch`는 이 플래그가 붙은 필드의 `mismatch`로 판정한다.

### 부재는 9값으로 구분한다

리포트의 침묵/명시 2분법 대신 상류 계약의 `unavailable_reason`을 그대로 쓴다:
`not_mentioned` / `source_document_missing` / `unreadable` / `outside_poc_scope` /
`route_not_activated` / `not_scheduled` / `printed_but_blank` / `conflict_unresolved` /
`partial_reading_only`. 문서종이 없어서 못 채운 값과 서식이 공란이라 못 채운 값은 다른 실패다.
`resolution_status`(`asserted`/`explicitly_absent`/`unavailable`/`not_applicable`/`conflict`)도 함께 기록한다.

### 추출 대상이 아닌 필드는 분모에서 뺀다

`extraction_wave: deferred`(6개) 및 `activation_basis: deferred`는 파이프라인이 의도적으로 추출하지
않는 필드다. 정답지에 값이 있어도 **감점하지 않고** `not_applicable`로 기록한다. 반대로
`extraction_wave`가 A/B인데 산출물에 항목 자체가 없으면 그것은 파이프라인 결함으로 보고한다
(현재 `documented_prior_condition_causation`·`documented_prior_condition_contribution_rate` 두 건이
설정상 wave B인데 네 케이스 모두에서 미출력).

| 판정 | 의미 |
| --- | --- |
| `exact` / `normalized` | 일치 (정규화 범위는 아래) |
| `mismatch` | 양쪽 값이 다르다 |
| `missing_in_screening` | 정답지에 있고 리포트에 없다 |
| `missing_in_ground_truth` | 리포트에만 있다 — 분모에서 제외 |
| `not_applicable` | deferred 필드 — 분모에서 제외 |

정규화 인정 범위: 날짜 표기, 좌우 표기(`LT`/`Lt.`/`좌측`), 금액 구분자, 진단명의 한자·괄호·공백
변형, 진단코드의 점 유무. 의미가 달라지는 축약은 정규화가 아니다.

```
F1 점수 = (exact + normalized) ÷ (exact + normalized + mismatch + missing_in_screening) × 100
```

### 법률의견 필드의 비대칭

`legal_authority: legal_opinion`인 필드(`liability_opinion_conclusion`, `comparative_negligence_rate`,
`negligence_reasoning`, `duty_breach_grounds`)는 **문서에 기재된 사실이 아니라 어느 당사자 대리인의
의견**이다. 상반된 두 의견서가 있는 것은 기록 모순이 아니며, 성립 의견은 유형을 성립시키지만
불성립 의견은 유형을 닫지 않는다. 리포트가 양쪽을 보존하고 판단을 유보한 것은 이 규칙을 따른 것이지
누락이 아니다 — `mismatch`로 잡지 말고 정답지가 채택한 쪽과 함께 기록한다.

## F2 · 쟁점 예측 리콜 — 가중 30

정답지가 실제로 다룬 쟁점을 분모로, 리포트가 미리 짚은 것을 분자로 둔다. **리콜이 이 차원의 점수다.**

- 정답지 쟁점 하나가 리포트의 어느 서술과 대응하는지를 스크리닝 쪽 인용으로 제시한다.
- 표현이 달라도 **같은 쟁점을 가리키면 예측한 것으로 본다.** 정답지가 "공작물 설치·보존상 하자"로
  쓰고 리포트가 "결빙 계단에 대한 시설 관리 책임"으로 썼다면 대응한다.
- 리포트에만 있는 쟁점은 `screening_only_issues`에 분류해 남긴다 — `손사_미기재` / `중복` / `오탐`.
  **어느 것도 점수에 반영하지 않는다.** precision은 기록만 한다.

```
F2 점수 = (예측한 정답지 쟁점 수 ÷ 정답지 쟁점 총수) × 100
```

## F3 · 필요서류 일치 — 가중 20

**분모도 설정에서 온다.** `required_documents_by_case_type` — 개인보험 6종 / 교통사고 12 / 산재 13 /
배상 14. 케이스의 판정된 유형에 해당하는 목록을 쓴다. 문서종 이름은 설정의 `document_kinds` 어휘를
그대로 쓴다(`diagnosis_certificate`, `initial_visit_record`, …).

| 판정 | 조건 |
| --- | --- |
| `correct` | 정답지가 쓴 문서를 리포트가 `보유`로 분류 / 정답지가 안 쓴 문서를 `부족`으로 지적 |
| `under` | 정답지가 쓴 문서를 `미확인`·`부족`으로 분류 |
| `miss` | 정답지가 쓴 문서를 언급조차 안 함 |
| `over` | `부족`으로 지적했으나 정답지 판단에 불필요 — 기록만, 감점 없음 |

상류가 `ambiguous`(Stage 2가 문서를 봤으나 종류를 못 정함)와 `missing`을 구분해 기록하므로,
'문서가 없어서 미확인'과 '분류를 못 해서 미확인'을 같은 under로 묶지 않는다.

```
F3 점수 = correct ÷ (correct + under + miss) × 100
```

## 우주 밖 항목 — 분모에 없는 것을 기록하는 자리

분모를 파이프라인에 묶으면 **파이프라인이 모르는 것은 잴 수 없게 된다.** 실제로 발생했다:
정답지가 사고경위의 근거로 삼는 `사고경위서`는 `document_kinds`와 `required_documents_by_case_type`
어디에도 없다(프로그램으로 확인). 그대로 바인딩하면 CASE_705·CASE_711에서 각각 잡았던 그 결손이
**측정 불가능해진다.**

그래서 정답지가 근거로 삼았는데 대응하는 `field_id` 또는 `document_kind`가 존재하지 않는 항목은
`out_of_universe_items`에 별도로 기록한다. 점수에는 반영하지 않는다 — 리포트의 잘못이 아니라
**설정의 결손**이고, 고칠 자리가 다르다.

## F4 · 결론 방향 일치 — 헤드라인 제외

| 판정 | 의미 |
| --- | --- |
| `aligned` | 리포트의 1차 판단 방향이 정답지 최종 결론과 같다 |
| `partial` | 방향은 같으나 범위·정도가 다르다 |
| `opposed` | 반대 방향이다 |
| `screening_declined` | 리포트가 방향을 단정하지 않았다 — **실패가 아니다** |

`screening_declined`가 감점이 아닌 이유는 F1의 재량값과 같다. 결론 방향은 재량 영역이고,
자료가 부족한 단계에서 단정하지 않는 것이 P3가 요구하는 처신이다. **그래서 헤드라인에 넣지 않고
따로 기록한다.**

# 4. 점수와 판정

```
fidelity_score = Σ적용가능(차원점수 × 가중) ÷ Σ적용가능(가중)
```

적용 불가한 차원(정답지에 해당 정보가 없는 경우)은 `applicable: false` + 사유로 기록하고
분모에서 뺀다. 차원을 배열에서 빼는 것은 스키마가 거부한다.

**핵심 필드 불일치는 점수와 무관하게 판정을 내린다.**

핵심 필드 = 주진단명 · 사고일 · 청구 담보 · 부지급·감액 사유. 이 중 하나라도 `mismatch`면
`core_field_mismatch: true`이고 판정은 **`divergent`로 고정**된다. 진단명이 틀린 리포트를
"80% 부합"으로 부르지 않기 위해서다. `missing_in_screening`은 여기에 해당하지 않는다 —
틀리게 쓴 것과 못 쓴 것은 다르다.

| 조건 | 판정 |
| --- | --- |
| `core_field_mismatch` | `divergent` |
| `fidelity_score >= 80` | `aligned` |
| `>= 60` | `partial` |
| 그 외 | `divergent` |

# 5. 기록 규칙

- 적용되는 모든 차원은 **스크리닝 리포트 원문 인용을 최소 1개** 단다. 스키마가 강제한다.
- 정답지 쪽은 `ground_truth_ref`(문서명 + 위치)를 단다. 인용 필드는 존재하지 않는다.
- `target_report_sha256`은 채점한 파일의 실제 해시를 복사한다. 기억으로 적지 않는다.
- 결과는 `screening_fidelity_result_v{n}.json`, `schemas/screening_fidelity_result.schema.json` 준수.

```bash
python tools/dao.py write-verification-result CASE_ID screening_fidelity_result_v1.json \
  --caller-stage screening_fidelity --version screening \
  --data-file <경로> --held-by screening-fidelity --run-id RUN_ID
```

파일명은 `screening_fidelity_result_v<n>.json`만 허용된다. 이 경로는 그 계약 하나를 위한 것이지
정답지 트리에 임의 파일을 쓰는 통로가 아니다.

# 6. 채점자가 하지 말아야 할 것

- 정답지 문장을 결과 파일·응답 어디에도 옮겨 적지 않는다. 필드값과 위치 참조만 남긴다.
- 사건을 다시 판단하지 않는다. 어느 쪽이 옳은지가 아니라 **부합하는지**를 본다.
- 정답지에 없는 값을 리포트가 담았다고 감점하지 않는다.
- 리포트가 범위를 한정해 부재를 밝힌 항목이라도, 정답지에 값이 있으면 일치율에서는 불일치다.
  성격만 `declared`로 구분해 남긴다.
- 인용 없이 점수를 주지 않는다.

# Citations

[1] [harness-guardrails-dev](../.claude/skills/harness-guardrails-dev/SKILL.md) D1
[2] [참조 없는 작성 품질 루브릭](rubric-screening-report.md)
[3] `schemas/evaluation_result.schema.json` — fact/discretionary 분리의 선례
