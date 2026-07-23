# 인수인계 프롬프트 — CASE_030 보험약관 정규화 이어가기

> 새 세션에서 아래 "프롬프트" 블록을 그대로 붙여넣으면 이어서 작업할 수 있습니다.
> 이 파일 자체는 `feature/policy_check` 브랜치 루트에 있습니다: `_handoff_policy_check.md`

---

## 전체 보험약관 정규화 결과는 어디에 있나

**단일 통합 파일은 없습니다.** 하네스 규약(문서당 1파일)에 따라 4개 파일로 나뉘어 있습니다.
모두 `outputs/`·`data/`는 **gitignore 대상**(버전관리 안 됨, 디스크에만 존재)입니다.

### 최종 정규화 결과 (담보/조항 → 표준필드)
| 파일 | 구간 | clause | refs |
|---|---|---|---|
| `outputs/CASE_030/normalized_policy_clause_DOC_001.json` | 보통약관 제3~5조(지급·면책·후유장해 세부) | 3 | 15 |
| `outputs/CASE_030/normalized_policy_clause_DOC_004.json` | 특별약관 핵심 4건(업무외 상해 사망/후유장해 + 보상제외 2건) | 4 | 13 |
| `outputs/CASE_030/normalized_policy_clause_DOC_005.json` | 특별약관 나머지 23건(골절·화상·식중독·감염병·깁스·응급·근골격·창상봉합) | 23 | 78 |
| `outputs/CASE_030/normalized_policy_clause_DOC_006.json` | 보통약관 제10~41조 중 denial 관련 8건(알릴의무·해지·환급·시효) | 8 | 22 |
| **합계** | | **38** | **128** |

### 정규화의 출처 (처리 원문, 인용 verbatim 대조 대상)
- `data/processed/CASE_030/DOC_001/` (page_001~012.md + redacted_text.md)
- `data/processed/CASE_030/DOC_004/` (page_057/058/118/119.md + redacted_text.md)
- `data/processed/CASE_030/DOC_005/` (page_059~117.md, 59쪽 + redacted_text.md)
- `data/processed/CASE_030/DOC_006/` (page_024~055.md, 32쪽 + redacted_text.md)
- 페이지 번호 = 문서 하단 "N / 240" **실제 논리 페이지**(DOC_004/005/006). DOC_001만 array순번 1~12(=논리 12~23).

### 검수 결과 (아티팩트, claude.ai 호스팅)
- 검수 표본: https://claude.ai/code/artifact/e87a9333-5d03-4a2e-a925-bbe1676a66f5
- 전체 검수 결과(지적 9건, 치명 0): https://claude.ai/code/artifact/838c5e37-17a8-47c7-aa4e-d53e23bc468e

### 버전관리되는 코드/스키마 변경 (git status에 보임, 아직 커밋 안 됨)
- `tools/_cross_contract.py` (신규) — provenance 강제화 로직
- `tools/dao.py` — write-contract에 cross-contract 게이트 훅
- `schemas/normalized_policy_clause.schema.json` v0.2 — strict_evidence_reference
- `schemas/common_component_output.schema.json` — strict_evidence_reference $def + application_form enum
- `tools/run_checkpoint1.py` — application_form 분류 개선
- `tests/test_cross_contract.py`, `tests/test_document_type_taxonomy.py` (신규)
- `pipeline.md` — taxonomy 갱신

---

## 프롬프트 (새 세션에 붙여넣기)

```
E:\SNU\intern\harness-project 의 feature/policy_check 브랜치에서 CASE_030(삼성화재 미니생활보험) 보험약관 정규화 작업을 이어간다.

먼저 읽고 준수: CLAUDE.md, .agents/skills/harness-guardrails/SKILL.md, harness-guardrails-dev/SKILL.md, .claude/agents/policy-pipeline.md, tools/_cross_contract.py(핵심 provenance 로직).

절대 제약(그대로 유지):
- 원본 PDF(data/raw), source-cases 최종 손해사정 보고서, data/ground_truth 를 직접 읽거나 수정하지 마라.
- outputs/, data/, ledger, run-state 를 직접 읽거나 쓰지 마라 — 모든 case 데이터 접근은 tools/dao.py 를 사용하라.
- policy-pipeline 은 read-document-text 가 반환한 redacted text 만 사용하라. write-contract 의 canonical stage name 은 policy_clause_processing.
- 인간 검토 결과나 ground truth 를 스스로 생성하지 마라. 사용자 변경사항을 되돌리지 마라.
- .claude 정본을 수정한 경우에만 tools/sync_agents.py 를 실행하라(생성본 .agents/.codex 는 손대지 마라).

현재 상태:
- 보험약관(DOC_001, 240p)의 보통약관+특별약관 전 구간을 4개 파일로 정규화 완료:
  outputs/CASE_030/normalized_policy_clause_{DOC_001,DOC_004,DOC_005,DOC_006}.json
  총 38 clauses, 128 evidence refs. 전부 strict schema PASS + cross-contract PASS.
- 특별약관/보통약관 뒷부분은 PDF 임베디드 텍스트 레이어가 있어 vision OCR 없이 결정적 디코드(extraction_method=embedded_text)로 처리했다. 별표(장해·골절·화상 분류표, 논리 p.120~240)는 참조표라 정규화 대상에서 제외했다.
- 페이지 번호는 문서 하단 "N / 240" 실제 논리 페이지 기준(DOC_004/005/006). DOC_001만 array순번 1~12.

검수에서 나온 지적 9건(치명 0)이 있다. 각 지적의 "누락 원문"은 실제 처리 텍스트에 verbatim 존재함을 확인했다. 다음 중 원하는 것을 진행하라(내가 지시할 것):

  [수정 반영 후보]
  A-1 화상진단비(DOC_005 C-3): 「1사고 1회 한도」 감액조건 추가 (원문 p.63에 존재, 다른 진단비 특약과 일관되게)
  A-2 특정감염병(C-6)·식중독(C-5): 「사망 시 진단확정일 소급 지급」 세부규정 추가 (p.70/69)
  A-3 수술 특약 8건(C-2,C-15~C-22): 「수술 정의 제외 시술 7종」을 exclusion으로 추가 (p.61 등)
  A-4 창상봉합술(C-23): A/B 지급등급 차등·제3자 의료자문·특약 소멸 규정 반영 (p.113~117)
  B-1 고지의무(DOC_006 C-1) payout 오분류 재검토
  C-1~C-3 인용 완결성(단어중간 절단, 조건절 소실) 확장

수정 방법: 해당 normalized_policy_clause_DOC_00X.json 을 읽어(dao.py read-contract) 수정 후, 반드시 각 인용문이 처리 원문(data/processed/CASE_030/DOC_00X/redacted_text.md)에 whitespace-정규화 후 verbatim 존재하는지 확인하고, tools/dao.py write-contract 로 재영속화하라(스키마 + cross-contract 게이트가 자동 재검증). 게이트가 거부하면 저장되지 않는다. 커밋은 내가 요청할 때만.

먼저 4개 파일과 처리 원문을 대조해 현재 상태를 확인한 뒤, 어떤 지적부터 반영할지 물어보고 진행하라. 계획부터 세워라. 한 보험사의 문서만으로 다른 보험사 형식까지 일반화하지 마라.
```

---

# CASE_030 파이프라인 구조 개편 진행 로그 (2026-07-24~)

목표: CASE_030에서 실제 발견된 오류들이 향후 보험약관에서도 자동 게이트를 통과하지
못하도록 구조적으로 차단한다. Part 1~7 = 코드/스키마/테스트, Part 8 = CASE_030 데이터
마이그레이션(DAO 전용, gitignore), Part 9 = e2e 회귀 + 문서 동기화. 각 Part는 독립 커밋.

테스트 기준선(작업 시작 시): 415 passed, 1 pre-existing FAIL(윈도우 path-sep 버그,
test_dao_forbidden_expr — 이번 Part 1에서 as_posix()로 함께 수정). frontend 3개 파일은
fastapi 미설치로 collection error(문서화된 기존 gap) — 실행 시 항상 제외.

## Part 1 — 명시적 stage dependency graph + 원자적 finalize (완료)

- 신규 tools/stage_dependencies.py: 파이프라인 의존성 그래프 단일 진실원.
  requires()(enum 인접이 아니라 실제 선행 stage), is_skippable()(현재 indexing만),
  check_dependencies(stage, target_status, state). 규칙: document_processing→intake(있을 때만),
  policy_clause_processing→document_processing, claim_analysis→policy_clause_processing,
  screening_report→claim_analysis+consistency_check. 미기록 optional stage를 "통과"로 간주하지
  않음. non-skippable stage는 skipped 불가.
- schemas/run_state.schema.json v0.2→v0.3: status에 skipped 추가; passed이면 backup_path
  non-null 필수(conditional). CASE_030의 "passed인데 backup_path=null" 구조적 불가.
- tools/dao.py:
  - _update_run_state가 dependency validator를 통과(dep_check=hard=거부, soft=경고 후 contract 유지).
    status=passed는 finalize=True일 때만 허용 — 직접 passed 설정 거부.
  - write-contract --stage / patch-manifest-document --stage는 이제 passed가 아니라
    in_progress(soft dep check)만 표시. 한 번의 write는 stage 완료가 아님.
  - 신규 _build_snapshot_atomic(temp 조립 후 atomic rename — 부분 백업 없음),
    _finalize_stage(dependency 선검사 → snapshot 빌드/검증 → 같은 run-state lock 안에서
    passed+backup_path+completed_at 기록). snapshot 실패 시 stage는 passed가 되지 않음.
  - snapshot-backup은 finalize-stage의 backward-compat 별칭. update-run-state choices에서
    passed 제거, skipped 추가. 신규 CLI finalize-stage.
- 테스트: 신규 tests/test_stage_dependencies.py(9), tests/test_dao_run_state.py에 Part 1 회귀 11건.
  기존 테스트 마이그레이션: fork/manifest_patch/run_checkpoint1의 passed seed를 finalize/backup_path로.
- 전체 434 passed(frontend 제외).

## Part 1 보강 + Part 2 — legacy migration, provenance, segment lineage (완료)

- 기존 v0.2 run-state가 `passed + backup_path=null`인 경우 새 v0.3 스키마로는
  어떤 정상 DAO 갱신도 할 수 없는 migration deadlock을 해소했다.
  `migrate-run-state-v03`은 유효한 백업을 날조하지 않고 legacy invalid pass를
  `failed`로 내린 뒤 append-only `migration_history`에 사유를 남긴다.
- finalize는 run-state lock을 유지한 채 dependency 확인 → prospective finalized
  state 생성 → snapshot publish → live run-state 기록을 수행한다. snapshot 안에도
  finalize된 `_run_state.json`이 들어가며, 이미 publish된 backup 경로는 삭제하거나
  덮어쓰지 않는다.
- `document_manifest` v0.5에 physical/segment 역할, parent, 비연속 page ranges,
  logical↔physical page map, derived-text SHA-256을 추가했다.
- 신규 `tools/segment_lineage.py`가 parent 존재/self-parent/cycle, range/page-map,
  processed marker, derived-text digest를 검증한다.
- normalized policy write는 manifest에 등록된 `insurance_policy` automated source만
  허용하며 write 시점마다 lineage를 다시 검증한다.
- physical automated document는 classification result가 없거나 manifest type과
  다르면 document processing을 finalize할 수 없다. Segment는 독립 classifier를
  가장하지 않고 parent document type을 상속한다.
- 회귀 결과: Part 1/2 관련 82 passed, frontend 의존성 테스트 2개 제외 전체
  `460 passed, 1 skipped`.
- CASE_030 실제 data/run-state migration은 아직 수행 전이다. 코드 커밋 후 DAO
  migration과 manifest segment 등록을 별도 데이터 작업으로 수행해야 한다.

## Part 3 — policy boundary inventory + completeness gate (완료)

- 신규 `policy_boundary_inventory_DOC_XXX.json` 계약은 processed policy page의 모든
  비공백 문자를 exact-offset span으로 분할한다. 각 span은 boundary에 속하거나
  `excluded`와 사유를 가져야 한다.
- `tools/policy_completeness.py`가 page/quote offset, overlap, 전체 문자 coverage,
  boundary source 연결, normalized clause/condition mapping을 cross-contract로 검증한다.
- 하나의 boundary span이 여러 제N조/①~⑳/번호 item anchor를 삼키지 못하게 하여
  같은 조항 안의 두 번째 항·호 누락이 별도 미처리 영역으로 드러나게 했다.
- 모든 automated insurance-policy document는 non-empty normalized contract와
  complete inventory를 모두 가져야 한다. `review_required` 또는
  `extraction_failed` boundary가 하나라도 있으면 `policy_clause_processing`
  finalize가 거부된다.
- `.claude/agents/policy-pipeline.md`와 pipeline.md를 새 계약에 맞게 갱신하고
  `tools/sync_agents.py`를 실행했다.
- 전체 회귀: frontend 의존성 테스트 2개 제외 `467 passed, 1 skipped`.

## Part 4~7 — stable policy addresses, evidence quality, tables, audit (완료)

- Part 4 (`6a48b66`): `clause_uid`/`condition_uid`를 불변 downstream
  주소로 도입하고, `clause_id`는 표시 전용으로 강등했다. 조항 종류별 전용
  semantic bucket과 cross-contract 의미 검사를 추가했다.
- Part 5 (`5be7b38`): 조건 근거를 direct/composite/insufficient로 명시하고
  제목만 인용하거나 내용 토큰이 거의 무관한 인용이 성공 계약으로 저장되는
  것을 차단했다.
- Part 6 (`7f28dc7`): 별표/분류표를 table/row/cell stable UID와 셀별 원문
  근거로 표현하는 `reference_table_DOC_XXX.json` 계약 및 조항 링크를 추가했다.
  복잡한 병합 셀의 완전한 2차원 순서는 known gap으로 남겼다.
- Part 7: `policy_audit_result_DOC_XXX.json`이 normalized/inventory/reference
  계약의 exact SHA-256에 묶인다. 누락·stale·open finding은 policy finalize를
  막고, downstream clause/condition UID도 current clear audit에 대해서만
  resolve된다.
