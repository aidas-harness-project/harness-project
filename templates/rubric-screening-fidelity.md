---
type: Reference
title: 스크리닝 리포트 정답지 대조 루브릭
description: 스크리닝 리포트가 실제 손해사정사 문서와 얼마나 부합하는지를 4개 차원으로 채점하는 PoC 기준.
tags: [rubric, screening, fidelity, evaluation]
timestamp: 2026-08-27T00:00:00+09:00
rubric_version: screening_fidelity.v0.2
---

> **v0.2 (2026-08-27) 변경점.** v0.1로 채점한 점수와 **직접 비교하지 않는다** — 분모가 달라졌다.
> 1. **F2 분모에서 PoC 범위 밖 쟁점을 뺀다** (`out_of_scope_amount` / `out_of_scope_policy`).
>    F1이 `deferred` 필드를 빼는 것과 같은 원칙이다. 총괄 리콜은 따로 기록한다.
> 2. **F1·F3 점수에 분모를 함께 기록한다.** 분모가 케이스마다 14~40(F1), 6~14(F3)로 달라
>    분모 없는 케이스 간 비교는 성립하지 않는다.
> 3. 근거 문서가 케이스에 있는데 라우팅 어휘가 없어 못 읽은 경우는 **범위 밖이 아니다** —
>    분모에 남기고 놓친 것으로 센다.
>
> v0.1 배치(eval_20260827, 39건)의 원본 점수는 그대로 보존한다. 재채점 없이 v0.2 수치와
> 나란히 인용할 때는 어느 판인지 반드시 밝힌다.

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

### 모든 필드가 행을 가진다 — 배제는 보이는 판단이어야 한다

`claim_analysis_result.json`이 해결한 필드는 **하나도 빠짐없이** `field_comparisons`에 행을 갖는다.
정답지가 말하지 않는 필드는 `missing_in_ground_truth`로 남기고 분모에서 뺀다. 빼는 것 자체는
여전히 판단이지만, 행이 존재하므로 **어떤 판단을 했는지가 기록에 남는다.**

이 규칙이 없던 동안 두 케이스가 각각 15행으로 채점됐고 어느 15행이었는지는 어디에도 없었다 —
같은 자로 잰 두 숫자가 아니었다. `dao.py write-verification-result`가 커버리지를 검사해
누락된 필드가 있으면 저장을 거부한다.

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

### F1도 분모를 함께 적는다 (v0.2)

실측(2026-08-27, 39건): 분모가 **14~40, 평균 25.2**다. 정답지가 대개 한 담보에 한정된 좁은
사정서여서 64필드 중 40개 가까이가 `missing_in_ground_truth`로 빠진다. 분모 14인 케이스는 한 행이
7.1점, 분모 40인 케이스는 2.5점을 움직이므로 **분모 없이 두 케이스의 F1을 비교하지 않는다.**
결과에 채점된 행 수를 기록한다.

### 법률의견 필드의 비대칭

`legal_authority: legal_opinion`인 필드(`liability_opinion_conclusion`, `comparative_negligence_rate`,
`negligence_reasoning`, `duty_breach_grounds`)는 **문서에 기재된 사실이 아니라 어느 당사자 대리인의
의견**이다. 상반된 두 의견서가 있는 것은 기록 모순이 아니며, 성립 의견은 유형을 성립시키지만
불성립 의견은 유형을 닫지 않는다.

리포트가 **양쪽을 보존하고 채택하지 않은 것은 이 규칙을 따른 처신**이다. 그런데 채택값이 없으니
`missing_in_screening`으로 잡히면 **F4가 `screening_declined`로 칭찬하는 행동을 F1이 감점**하게 된다.
그래서 판정값 `preserved_without_adoption`을 둔다 — **분모에서 빼되 행으로 남긴다.**

- 일치로 **치지도 않는다.** 리포트가 정답지의 값에 도달한 것은 아니다.
- **`legal_authority: legal_opinion`인 필드에만** 쓸 수 있다. 일반 필드가 값을 안 담은 것은 보존이 아니다.
- **보존을 보여주는 리포트 인용이 필수**다. 없으면 침묵을 면제로 바꾸는 통로가 된다.
- 리포트가 애초에 의견 자료를 담지 않았다면(CASE_711처럼 배상책임 사실 전부 `unavailable`)
  이 판정을 쓰지 않는다 — 보존할 것이 없었으므로 `missing_in_screening`이다.

세 조건 모두 `score_screening_fidelity.py`와 계약 스키마가 강제한다. **다만 인용문이 실제로 보존을
보여주는지는 검사하지 못한다** — 도구가 대신할 수 없는 판단으로 남는다.

## F2 · 쟁점 예측 리콜 — 가중 30

정답지가 실제로 다룬 쟁점을 분모로, 리포트가 미리 짚은 것을 분자로 둔다. **리콜이 이 차원의 점수다.**

- 정답지 쟁점 하나가 리포트의 어느 서술과 대응하는지를 스크리닝 쪽 인용으로 제시한다.
- 표현이 달라도 **같은 쟁점을 가리키면 예측한 것으로 본다.** 정답지가 "공작물 설치·보존상 하자"로
  쓰고 리포트가 "결빙 계단에 대한 시설 관리 책임"으로 썼다면 대응한다.
- 리포트에만 있는 쟁점은 `screening_only_issues`에 분류해 남긴다 — `손사_미기재` / `중복` / `오탐`.
  **어느 것도 점수에 반영하지 않는다.** precision은 기록만 한다.

### PoC 범위 밖 쟁점은 분모에서 뺀다 (v0.2)

F1이 `deferred` 필드를 `not_applicable`로 빼는 것과 **같은 원칙**이다. 파이프라인이 하기로 하지
않은 일을 못 했다고 감점하면, 그 점수는 리포트 성능이 아니라 범위 설정을 재는 것이 된다.

각 쟁점에 `scope`를 붙인다. **분모는 `in_scope`만이다.**

| `scope` | 뜻 | 분모 |
| --- | --- | --- |
| `in_scope` | PoC가 하기로 한 영역 | **포함** |
| `out_of_scope_amount` | 금액 산정 — 위자료, 일실수익, 소득 기준, 가동연한·중간이자, 치료비 인정액, 손해액·사정금액, 보험가입금액 | 제외 |
| `out_of_scope_policy` | 약관 해석 전제 — 면책사유, 담보 적용 여부, 약관상 지급사유 충족 | 제외 |

**위 목록의 낱말보다 `field_id` 조회가 우선한다.** 목록은 예시이지 판정 규칙이 아니다. 실제로
충돌한 사례: "장해분류표 지급률"은 약관 계열로 읽히지만 `documented_disability_rate`가
grade B · `critical_conflict_field: true`로 설정에 있으므로 **`in_scope`다**(CASE_8005에서
확인). 마찬가지로 "입원급여금 사정 범위"도 `admission_status`·`admission_period`가
추출 대상이면 그 전제는 범위 안이다.

두 제외 사유의 근거는 다르다. **금액**은 PoC 평가 축에서 명시적으로 빠져 있다(보험료 산출·사정금액은
평가 대상이 아니다). **약관**은 이 코퍼스의 상당수가 D5 무약관 선언 케이스여서 약관 원문이 존재하지
않고, 존재하지 않는 문서를 근거로 한 조항 해석은 파이프라인이 수행하도록 설계된 적이 없다.

**판정은 채점자가 쟁점 단위로 한다.** 라벨의 낱말로 기계적으로 가르지 않는다 — "치료비"가 들어갔다고
전부 금액 쟁점인 것은 아니고(치료의 의학적 타당성은 `in_scope`), "담보"가 들어갔다고 전부 약관
쟁점인 것도 아니다(어느 담보를 청구했는지의 사실 확인은 `in_scope`). **판단 근거를 `scope_reason`에
한 줄로 남긴다.**

경계가 애매하면 `in_scope`로 둔다. 제외는 점수를 올리는 방향이므로, 의심스러울 때 빼는 쪽으로
기울면 지표가 낙관적으로 망가진다.

**대응하는 `field_id`가 설정에 존재하면 범위 밖이 아니다.** 이것이 경계 판정의 1차 기준이다.
파이프라인이 그 값을 뽑기로 이미 정해둔 필드라면, 정답지가 그것을 금액 계산에 썼다는 사실은
범위를 바꾸지 않는다.

구체적으로 **과실비율(`comparative_negligence_rate`)은 언제나 `in_scope`다.** grade A이고
`critical_conflict_field: true`이며 루브릭이 "재량이 아니라 A등급 추출 대상"이라고 이미 명시한
필드다. CASE_705에서 정답지가 이를 위자료·일실수익 산식의 곱수로만 다뤘다는 이유로
`out_of_scope_amount`로 분류한 사례가 있었는데, **그것은 오분류다** — 정답지가 그 값을 어디에
쓰는지가 아니라 파이프라인이 그 값을 뽑기로 했는지가 기준이다. 같은 이유로 `legal_basis_cited`,
`liability_opinion_conclusion`도 `in_scope`다.

반대로 위자료 기준금액, 소득 기준, 가동연한·중간이자, 사정금액에는 대응 `field_id`가 없다
(채점자들이 `out_of_universe_items`로 반복 기록한 항목들이다). 그래서 이들은 범위 밖이다.

```
F2 점수 = (예측한 in_scope 쟁점 수 ÷ in_scope 쟁점 총수) × 100
F2 총괄 리콜(기록용) = (예측한 전체 쟁점 수 ÷ 전체 쟁점 총수) × 100
```

두 값을 **모두 기록한다.** 헤드라인은 `in_scope` 쪽이고, 총괄 리콜은 사정사 업무 전체 대비 커버리지가
얼마인지를 남기는 자리다. 둘을 합치지 않는다.

`in_scope` 쟁점이 하나도 없으면 F2는 `applicable: false`로 두고 가중 30을 분모에서 뺀다.

### 문서가 케이스 안에 있는데 못 읽어서 놓친 쟁점은 분모에 남는다

**제외 사유는 위 두 가지뿐이다.** 근거 문서가 케이스에 실제로 존재하고 분류까지 맞는데 라우팅
어휘가 없어 도달하지 못한 경우는 **`in_scope`이며 놓친 것으로 센다.** 고칠 수 있는 결함이고,
분모에서 빼면 지표에서 사라져 고칠 압력도 사라진다.

2026-08-27 코퍼스 실측: 미독 문서 108건 중 이 부류는 4건이다 — 사고경위서 2건(CASE_8040,
CASE_8033), 소방서 화재조사 보고서와 현장사진 2건(CASE_8033). 나머지는 분할 완료된 원본
번들 50건(정상), 증권 37건·위자료 기준표 10건·약관 2건·판례 1건·서식 4건(전부 범위 밖)이다.
`known-gaps.md` #69, #72 참조.

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

### F3에도 분모를 함께 적는다 (v0.2)

분모가 케이스 유형에 묶여 있어 **6종(개인보험)과 14종(배상)이 같은 척도가 아니다.** 실측
(2026-08-27, 39건): 6종 분모 19건 평균 F3 86.8 대 12~14종 분모 20건 평균 75.0 — 12점 가까운
차이가 리포트 품질이 아니라 유형 구성에서 온다. 그래서 F3 점수를 적을 때 **판정된 케이스 유형과
채점된 문서종 수를 함께 기록한다.** 그것 없이 케이스 간 F3를 비교하지 않는다.

또한 `document_kind`가 존재하지 않는 문서는 **체크리스트에 빠뜨릴 자리 자체가 없다.** F3 100은
"설정이 표현할 수 있는 범위에서 전부 맞혔다"이지 "정답지가 쓴 문서를 전부 요청했다"가 아니다.
그런 문서는 아래 `out_of_universe_items`로 간다.

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
- 점수 계산은 `python tools/score_screening_fidelity.py --rows <행 파일> --run-id RUN_...`이 한다.
  채점자가 만드는 것은 비교 행뿐이고, 등급·핵심 여부·deferred·부재 사유·가중치·판정은 도구가
  설정과 계약에서 읽는다.
- **재채점은 덮어쓰지 않는다.** 파일명이 실행을 담는다 —
  `screening_fidelity_result_<RUN_ID>.json`. 이미 있는 파일에 쓰면 거부된다.
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
