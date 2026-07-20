# Stage 1 재설계: Case Intake & Document Segmentation

## Context

현재 파이프라인은 **원본 PDF 1개 = 논리 문서 1개(DOC_XXX)**로 취급한다. 그러나 실제
`source-cases/`의 자료는 그렇지 않다:

| 페이지 | 파일 |
|---|---|
| 110p | 배상-상완골 근위부 골절OP |
| 77p | 배상 한화손보 손해사정서 |
| 59p | 배상 손해사정서 |
| 21~23p | 뇌혈관질환진단비 (4개 보험사) |

번들 하나에 청구서·진단서·의무기록·영수증·보험사 회신이 **연속으로 이어붙어** 있다.
단일 문서로 OCR하면 하위 단계 전체(분류, 필드추출, 근거인용)가 잘못된 문서 경계 위에서
동작한다.

**목표**: OCR 이전에 번들을 논리 문서 단위로 분할한다. Stage 1의 산출물은 텍스트가
아니라 **문서 구조**다.

Stage 1이 하지 않는 것: full OCR, 원문 전사, 최종 document type 확정, redaction, chunking.

---

## 사전 실측으로 확인한 사실 (설계 근거)

추측이 아니라 실제 PDF로 측정한 값이다.

### 1. 값싼 텍스트 기반 경계 탐지는 불가능

```
110p 번들: embedded text가 있는 페이지 = 0 / 110 (9개 PDF 전부 동일)
```

전 페이지가 스캔/캡처 이미지. 파일명·임베디드 텍스트 기반 경계탐지는 원천 배제.
→ **비전 기반 접근이 유일한 선택지**임이 데이터로 확인됨.

### 2. 구조적 신호(이미지 개수/크기)로도 경계를 못 찾음

페이지별 embedded image 개수·최대 오버레이 크기를 전 페이지 스캔한 결과, 거의 모든
페이지가 "변화"로 표시되어 신호 대 잡음비가 쓸 수 없는 수준(2→24개 급변이 문서 경계가
아니라 캡처 방식 차이에서 발생). → LLM 없는 무료 사전필터는 포기.

*이 음성 결과를 기록하는 이유: 나중에 "왜 더 싼 방법을 안 썼나"에 대한 답이다.*

### 3. 세로 스트립 합성은 자기모순 — 격자로 변경 확정

Claude vision은 **긴 변을 1568px로, 총 픽셀을 ~1.15M로 제한**한다. 세로로 이어붙이면
세로가 긴 변이 되어, 세로를 1568에 맞추는 과정에서 **가로 폭이 함께 뭉개진다.**

| 방식 | 최종 크기 | 페이지당 토큰 |
|---|---|---|
| 세로 스트립 15p | **222 × 1568 — 폭 붕괴** | 31 |
| 세로 스트립 8p | 416 × 1568 | 108.8 |
| 4×4 격자 16p | 1568 × 731 | 95.8 |

격자는 어떤 배치든 약 1533토큰으로 **상한이 동일**(픽셀 예산에 걸림). 따라서 한 장에
많이 넣을수록 페이지당 토큰이 순수하게 싸진다. **4×4 격자는 8p 세로 스트립보다 페이지당
토큰이 싸면서 동시에 2배를 담는다.** 세로 스트립은 토큰 절약이라는 목적 자체에 대해
격자보다 열등하다. → **격자 방식으로 변경 확정.**

### 4. 격자 크기 — 실제 시트를 렌더링해 **4×4로 확정** (3단계)

**해결됨.** 실제 번들의 첫 시트를 2×4/3×4/4×4로 렌더링해 눈으로 비교한 결과
**4×4로 충분히 판독됩니다.** 387×177 셀에서 제목(손해 사정서, 진 단 서,
후유장해진단서), 레터헤드, 심지어 본문 문단까지 명확히 읽힙니다. 첫 시트에서
**실제 문서 경계도 직접 보였습니다** — p1~13은 같은 손해사정 레터헤드, p14에서
진단서로 전환. 이 단계가 찾으려는 바로 그 신호입니다.

이전의 우려(387px에서 항목이 뭉개짐)는 **렌더 후 축소하는 방식**을 가정한 것이었고,
zoom 매트릭스로 **셀 크기에 직접 렌더**하면서 그 손실이 없어져 해당하지 않게 됐습니다.
3×4는 확연히 크므로 어려운 번들에서는 플래그 하나로 전환 가능합니다.

아래는 이 결정이 왜 논쟁적이었는지에 대한 기록입니다.

#### 원래 분석 — 계산과 실물이 엇갈렸던 지점

계획 수립 중 **판독성 지표에 결함**이 발견됐다. 기록해둔다.

산수상 4×4의 16pt 제목은 10.5px다(독립 재계산으로 재현됨). 그러나 실제 페이지를
렌더링해 육안 확인한 결과, **387px 셀에서는 섹션 제목은 읽히지만 그 아래 항목이 이미
뭉개진다.** 544px(2열)에서는 항목까지 편안히 읽힌다.

원인: "16pt 제목 크기"라는 지표가 **판독성을 대표하지 못했다.** 문서 종류 판별에는
제목뿐 아니라 그 아래 양식 구조·항목도 필요하고, 그건 더 작은 글씨다.

| 격자 | p/장 | 셀 | 16pt 제목 | 토큰/p | 110p |
|---|---|---|---|---|---|
| 2×4 | 8 | 555×259 | 14.9px | 192 | 14장 |
| **3×4** | 12 | 453×211 | 12.2px | 128 | 10장 |
| 4×4 | 16 | 392×183 | 10.5px | 96 | 7장 |

**→ 3×4를 잠정 기본값으로 두되, 격자는 `--grid COLSxROWS` 플래그로 조정 가능하게
만든다. 실제 시트를 눈으로 본 뒤 확정한다** (구현 순서 3단계에서 강제 중단점).
하드코딩 금지 — 이건 튜닝 결정이지 재작성 대상이 아니어야 한다.

### 5. 렌더 DPI는 최종 품질에 무관 — 낮게 렌더하고 직접 리사이즈

```
dpi 100/150/200 → 어느 쪽이든 최종은 동일 (긴 변 제한이 병목)
```

고DPI 렌더링은 순수한 낭비. **셀 크기에 맞춰 `fitz.Matrix(zoom, zoom)`로 한 번에
렌더**하면 중간 고해상도 PNG 자체가 불필요하다. 기존 `ocr_extract.split_to_page_images`는
DPI가 양쪽 백엔드에 하드코딩(ocr_extract.py:188, :226)되어 있고 **P8 OCR 경로의
품질 상수**이므로 건드리지 않는다 — Stage 1은 독자 렌더링한다.

### 6. 콘텐츠는 페이지 상단에서 시작 — 단, 예외 있음

오버레이 bbox 세로 위치 측정 결과 본문이 **페이지 높이의 0.02~0.20에서 시작**.
상단 1/3 크롭 안에 문서 시작부가 들어온다. 예외 둘:
- 오버레이가 없는 빈 페이지 존재 → 빈 크롭 예외 처리 필요
- **번들의 약 절반이 90도 회전되어 스캔되어 있습니다**(p29~73 부근의 긴 구간과
  뒤쪽의 짧은 구간). 전 구간 `page.rotation`은 0 — 콘텐츠 방향이지 PDF 플래그가
  아니라 메타데이터로는 알 수 없습니다.

  **해결책은 아무것도 하지 않는 것입니다.** 스캔된 방향 그대로 모델에 보내고,
  프롬프트에 "일부는 회전되어 있다"는 한 줄만 넣습니다. p65~80(p65~73 회전)을 담은
  실제 4×4 시트로 검증한 결과, 모델이 **회전된 9칸 전부를 읽었고 못 읽은 칸은 0**,
  **p74 경계를 confidence 0.92**로 찾으면서 근거로 "방향과 레이아웃이 급격히 바뀜"을
  들었습니다. p65에 대해서는 confidence 0.45로 낮추고 `needs_full_page`에 올렸는데,
  상단 조각으로는 앞 시트의 p64에서 이어지는지 알 수 없기 때문으로 정확한 판단입니다.

  회전 복원은 검토 후 기각했습니다 — **어느 방향으로 돌아갔는지 판별하는 데 실패**했고
  (회전이 확실한 9장 중 3장 오판, 정상 대조군은 기준선 자체가 없음), 양방향 수록은
  시트가 +57%~+100% 늘어 토큰 절약이라는 설계 목적과 충돌합니다.

  **모든 시트를 세 벌로 렌더링합니다: 원본 그대로, +90°, −90°**
  (`build_sheet_set`, `SHEET_VARIANTS`). 어느 방향으로 돌아갔는지 판별할 수 없으므로
  세 벌을 다 만들고, 읽는 쪽(수동 검토하는 사람이든 모델이든)이 읽히는 것을 쓰면
  됩니다. 렌더링은 시트당 약 0.2초로 싸고, 모델 호출은 시트당 한 벌만 보내므로
  추가 비용이 없습니다. 파일명에 변형이 들어갑니다(`sheet_02_p033-048_cw.png`).

  **이것이 수동 검토를 가능하게 하는 핵심입니다.** 회전 구간의 원본 시트는 제목 대신
  표의 왼쪽 가장자리만 보여주므로, 회전본이 없으면 번들의 절반에 대해 검토자가 손쓸
  방법이 없습니다. 실제 p33~48 시트로 확인 — 원본은 판독 불가, `_cw` 변형에서는
  p36·p43의 "진료비 세부내역서" 제목이 선명합니다.

  **회전 감지기는 넣지 않습니다.** 한때 있었으나 삭제했습니다. 세 변형이 모두 있는
  이상 "이 페이지는 눕었을 수 있음"이라는 플래그는 무엇을 막지도, 변형하지도 않고
  로그 한 줄만 남기면서도 보정에는 실제 노력이 들었습니다(렌더 크기에 따라 흔들리는
  측정치에 맞춰 임계값을 두 번 조정했고, 정상 페이지 오탐은 사람이 결과를 읽고서야
  발견됐습니다). **아무도 그에 따라 행동하지 않는 조건을 감지하는 것은 기능이
  아닙니다.**

### 7. 전체 코퍼스 토큰 효과

344페이지 기준, 3×4 격자로 29장 → 약 44,000토큰.
페이지별 개별 호출(344회, ~527,000토큰) 대비 **약 92% 절감**. 목적이 달성된다.

---

## 브랜치 전략

`fix_codex` → `feature/stage1`으로 분기 (사용자 확정).

근거: `fix_codex`는 `main`을 이미 merge한 상위집합(`a3f60cf`)이며, Stage 1이 재사용할
`tools/llm_providers.py`(884줄, 신규)와 `ocr_extract.py` 개선이 `fix_codex`에만 있다.

**충돌 위험 관리:**
- `outputs/`, `data/`는 이미 gitignore(.gitignore:22,28) → 실행 산출물은 커밋에 안 들어감
- 주 산출물은 **신규 파일** → 충돌 표면 최소
- **유일한 실질 충돌 지점: `tools/dao.py`.** main이 `check-forbidden-expressions`를
  파서 목록 중간(987행)에 추가했다. → 새 서브커맨드는 **목록 맨 끝에만 추가.**

---

## 구현 계획

### A. 신규 스키마 `schemas/segmentation_proposal.schema.json`

`outputs/CASE_XXX/segmentation_proposal_{source_doc_id}.json` — 번들당 1파일
(`ocr_result_{doc_id}.json` 선례: 공유 파일명은 두 번째 쓰기가 첫 번째를 파괴한다).
공유 리뷰 상태이므로 `source_ledger.schema.json`처럼 common output 봉투를 쓰지 않는다.

주요 필드:
- `case_id`, `source_document_id`, `source_file_name`, `source_file_path`(`^data/raw/`),
  `source_page_count`, `review_status`(`pending|approved|rejected`),
  `reviewed_by`/`reviewed_at`/`rejection_reason`
- `unassigned_pages[]` — 어느 세그먼트에도 안 속한 페이지를 **명시 데이터로** 보관
  (리뷰어가 계산하게 하지 않는다)
- `method`: **`ocr_performed`는 `{"const": false}`** — Stage 1이 OCR을 안 한다는
  구조적 보증. 스키마가 OCR을 주장하는 proposal을 거부한다. 가장 값싼 경계 강제.
  그 외 `method_version`, `mode`(`manual|vision_proposal` — B/C는 둘 다
  `vision_proposal`이고 provider 필드로 구분. 백엔드를 mode로 인코딩하지 않는다),
  `provider_name`/`model_name`/`prompt_version`/`provider_metadata`,
  `render_dpi`, `crop_ratio`, `grid_cols`/`grid_rows`,
  `sheet_pixel_budget{long_edge,total_pixels}`(토큰 비용 회귀를 산출물만으로 진단 가능),
  `contact_sheets[]`, `full_page_fallback{triggered,pages,saturated,cap}`
- `segments[]`: `segment_index`, `page_start`/`page_end`(양끝 포함),
  `provisional_document_type`(enum|null), **`provisional_type_label`(자유 텍스트)** —
  닫힌 enum에 맞는 항목이 없을 때 강제하면 정보가 소실되므로 모델의 표현을 보존,
  `confidence`, `boundary_evidence`(사람이 짧은 시간에 검토 가능하게 하는 핵심),
  `review_status`(`pending|approved|edited|rejected`), `needs_full_page`,
  `orientation_suspect`, `assigned_document_id`

JSON Schema로 "연속·비중첩·범위 내"를 표현할 수 없다 → `validate_segments()`(순수 함수)로
강제. `allOf`로 흉내내지 않는다.

### B. `document_manifest.schema.json` v0.4 → v0.5

추가 (owner: Case Intake/Segmentation):
`source_file_name`, `source_page_start`, `source_page_end`,
`segmentation_proposal_path`, `provisional_document_type`

**`provisional_document_type`에 명시적 주석 필수**: *"OCR 이전 시각적 추정. 신뢰 금지.
checkpoint 1이 `document_type`을 소유하며 자체 분류를 수행해야 한다 — 사람이 단언한
`pre_flagged_type`과 달리 이것은 신뢰 대상이 아니다."* 이 구분을 적어두지 않으면
누군가 분류 단축경로에 연결한다.

좁은 조건부 하나: `source_page_start`가 있으면 `source_file_name`/`source_page_end`도
필수(페이지 범위가 반쪽으로 존재하지 않게). `page_end >= page_start` 비교는 JSON
Schema가 못 하므로 도구 책임.

`required`는 그대로 — 기존 manifest를 무효화하지 않기 위해. 버전 상승 후
`validate_output.py`로 기존 manifest 회귀 검사.

### C. `tools/segment_case.py`

**CLI (서브커맨드):**
```
sheets  CASE_ID DOC_ID [--crop-ratio 0.33] [--grid 3x4] [--dpi 110]
propose CASE_ID DOC_ID [--mode manual|vision] [provider args] [--resume]
show    CASE_ID DOC_ID
approve CASE_ID DOC_ID --reviewer NAME [--segment N] [--edit N=start-end]
split   CASE_ID DOC_ID --held-by NAME --run-id RUN_ID
```
`sheets`가 Mode A이자 PoC 기본값: 시트 렌더 + 빈 proposal 골격 작성 + 경로 출력 후 정지.
**LLM 호출 0.**

**순수 함수(테스트 용이):**
`compute_sheet_geometry(...)` (양쪽 상한 인코딩, 경계값 테스트),
`plan_sheets(page_count, per_sheet)`,
`parse_segmentation_response(raw, sheet_pages)` (`redact_document._parse_redaction`의
`JSONDecoder().raw_decode` 스캔 루프 차용),
`validate_segments(segments, page_count)`,
**`merge_sheet_proposals(per_sheet, page_count)`**,
`build_manifest_entries(...)`

#### 2단계 상세: 파서의 실패 방식 (기존 두 선례가 갈린다)

- `redact_document._parse_redaction`은 **예외를 던진다**(`ProviderExecutionError`)
- `intake_case._parse_content_scan_verdict`는 **dict 반환 + fail-safe**

세그먼테이션은 **후자**를 따른다. 한 시트의 파싱 실패가 나머지 시트까지 죽이면 안 되고
(비싼 비전 호출을 이미 지불했다), 계획의 "경계를 발명하지 않는다" 원칙상 실패는
"그 시트가 다루는 페이지를 `unassigned_pages`로 남기고 경고"로 표현되어야 한다.
→ `parse_segmentation_response`는 `{"ok": bool, "boundaries": [...],
"continuations": [...], "needs_full_page": [...], "warning": str|None}` 반환.
**예외를 던지지 않는다.**

단, `raw_decode` 스캔 루프 자체는 `_parse_redaction`에서 그대로 차용한다
(후행/선행 산문 내성이 필요한 이유는 동일하다).

#### 2단계 상세: `merge_sheet_proposals` 알고리즘

핵심 통찰: **세그먼트 경계는 `boundaries`의 합집합만으로 결정되고, 시트 경계는 아무
의미가 없다.** `continuations`는 커버리지 검증용이지 분할 근거가 아니다. 이렇게 보면
"시트를 넘는 문서" 문제가 자동으로 사라진다.

```
시트1: p1-12   boundaries=[1]        continuations=[2..12]
시트2: p13-24  boundaries=[15]       continuations=[13,14,16..24]
올바름:  SEG(1-14), SEG(15-24)
틀림:    SEG(1-12), SEG(13-14), SEG(15-24)   <- 시트 경계에서 잘림
```

두 필드의 역할이 대칭이 아니다: **`boundaries`만 세그먼트를 만들고,
`continuations`는 "모델이 이 페이지를 실제로 봤다"는 커버리지 확인용**이다. 그래서
양쪽 어디에도 없는 페이지가 "모델이 빠뜨렸다"는 신호가 된다.

경계 사례 처리 **(4건 모두 사용자 확정)**:

| 사례 | 처리 | 근거 |
|---|---|---|
| **A.** p1이 boundary로 보고 안 됨 | **p1을 경계로 간주** + 경고 | 번들의 1페이지는 정의상 무언가의 첫 페이지다. 모델이 말하지 않아도 사실이 바뀌지 않는다 |
| **B.** 어떤 페이지가 양쪽에 다 없음 | **`unassigned_pages`로 남김** | 모델이 언급조차 안 한 페이지를 앞 문서에 조용히 편입시키면 사람이 그 사실을 모른 채 승인한다. split이 미할당에서 멈추므로 반드시 사람이 본다 |
| **C.** `needs_full_page` 페이지 | **full-page fallback으로 재확인**(§F) 후 그 답으로 확정. 2차에서도 판단 불가면 세그먼트에 `needs_full_page: true` 플래그를 남기고 사람에게 | 추측 대신 실제 근거로 판단한다. 3차 라운드는 없다 |
| **D.** 같은 페이지가 boundary이자 continuation | **boundary 우선** + `warnings`에 모순 기록 | 오류 비용이 비대칭이다. 과분할은 사람이 시트 보고 합치면 끝이지만, 과병합은 OCR·분류·필드추출이 전부 잘못된 경계에서 돈 뒤에야 드러나고 그때는 하위 산출물이 이미 오염돼 있다 |

`ok: false`인 시트가 섞여 있으면 **그 시트의 페이지 범위만** `unassigned_pages`로
가고, 나머지 시트의 경계는 정상 처리한다.

**오케스트레이션**: `segment_case(..., provider=None, progress=None, resume=True) -> dict`.
`run_checkpoint1.run_checkpoint1()` 계약 준수 — **provider 주입식, 예외 대신 status dict
반환, `sys.exit`은 `main()`에서만.** (`intake_case.scan_for_answer_key_content`는
라이브러리 코드에서 `sys.exit`하는 잘못된 선례 — 구조만 빌리고 이 점은 따르지 않는다.)

**스크래치**: 신규 `_segmentation_scratch/` (`.gitignore` 추가 필요).
**`ocr_extract.scratch_dir`를 쓰면 안 된다** — `finally`에서 rmtree하는데 시트는
사람 검토를 위해 프로세스보다 오래 살아야 한다.

### D. 컨택트 시트 합성 (Pillow 11.1.0, Malgun Gothic 확인됨)

- `zoom = cell_w / 595.0`으로 **셀 크기에 맞춰 직접 렌더** → 크롭 → 리사이즈 불필요
- 셀 사이 **빨간 구분선 4px** (3px는 다운스케일 후 노이즈로 읽힘). 순수 red(255,0,0) —
  스캔 문서에 채도 높은 빨강이 없어 최대 분리. **시트 가장자리 포함 전 셀을 완전히
  둘러싼다** (두 면만 막힌 셀이 "같은 문서 계속인가?" 모호성의 근원)
- **페이지 번호**: 셀 좌상단 빨간 칩 위 흰 글씨(임의 스캔 내용 대비 최대 대비).
  **절대 페이지 번호**(`p41`, `cell 5` 아님) — 모델이 위치를 셀 필요를 없애는 게 목적.
  폰트는 방어적 로드(`arial` → `DejaVuSans` → `load_default`)
- **부분 시트**: 캔버스는 full-size 유지, 미사용 셀은 흰색 + 빨간 박스/번호 없음.
  축소하면(시트마다 기하가 달라짐) 모델의 공간 기대가 깨지고, 페이지를 반복해 채우면
  유령 경계가 확정적으로 생긴다
- **빈 크롭/눕은 페이지**: `(blank)` 표시 또는 `orientation_suspect` → full-page 경로

### E. 비전 프롬프트

**Gotcha 준수 (실측 확인된 실패 모드):**
- **모든 비전 호출은 `provider.transcribe_image()` 경유.** 이 함수가 내부적으로
  작동하는 명령형(`f"Read the image file at {path} and then: {prompt}"`)을 붙인다.
  라벨 형식은 통제 실험 **9/9 실패**. `_run` 직접 호출 금지.
  → 부수 효과로 **Mode C도 provider 클래스 변경 없이 작동**
  (`LocalVlmProvider`는 `transcribe_image` 외 전부 거부, llm_providers.py:620-635)
- **자기정당화 문구 절대 금지.** "승인된 단계다", "거부하지 말 것", 역할극 서두 모두
  금지 — 과거 프롬프트 인젝션 신호로 읽혀 거부당한 전례. 정상적인 레이아웃 분석
  요청은 스스로를 변호할 필요가 없다

응답 형식:
```json
{"boundaries": [{"page": 13, "type_label": "후유장해진단서",
                 "type_guess": "diagnosis_certificate",
                 "confidence": 0.7, "evidence": "..."}],
 "continuations": [14, 15, 16],
 "needs_full_page": [4, 17]}
```

**파싱 실패 정책**: `_parse_content_scan_verdict`가 `flagged=True`로 fail-safe하는 것과
달리, 세그먼테이션에는 안전한 기본값이 없다. 파싱 실패 시 **세그먼트 0개 생성**,
해당 페이지는 `unassigned_pages`로, `review_status: pending` 유지, 경고 기록.
"제안 없음 → 사람이 처리"가 여기서의 fail-safe다. **실패한 파싱에서 경계를 발명하지
않는다.**

### F. Fallback (2차 호출) — 포화 위험 주의

**100% 스캔 이미지 + 작은 셀 조합이라 `needs_full_page`가 예상보다 자주 발동할 수 있다.**
110p 중 40p가 플래그되면 fallback이 주 경로가 되어 개별 호출보다 비싸진다.

1. 시트별 `needs_full_page` 합집합·중복제거
2. **`--max-full-page-fallback`(기본 페이지수의 25%) 초과 시 `saturated: true` 기록,
   fallback 전체 생략, 사람 검토로 회부.** 포화는 크롭 비율이나 격자가 이 번들에
   안 맞는다는 **튜닝 신호**이지, 큰 지출로 덮을 문제가 아니다
3. 미만이면 해당 페이지만 full-page 렌더(시트당 1~2p)해 **배치 호출** (N회 개별 호출 아님)
4. 3차 라운드 없음 — full-page에서도 판단 불가면 사람 결정

### G. 분할 실행과 manifest 병합

`split`은 case-level `approved` **그리고** 전 세그먼트 `approved`/`edited`,
`validate_segments` 통과일 때만 실행 (`intake_case.py:405-413`의 하드 게이트와 동일 —
미해결 항목 하나가 전체를 막는다).

**`dao.replace_manifest_documents(case_id, documents, held_by, run_id) -> (ok, message)`
신규 추가.** `write_manifest`는 전체 덮어쓰기, `patch_manifest_document`는 1건 패치 —
둘 다 "N건 추가 + 1건 은퇴"를 못 한다. `patch_manifest_document`의 튜플 반환 규약을
따르고, **읽기를 락 안에서** 수행한다 (known-gaps item 7이 존재하는 이유).

**번들 엔트리는 삭제하지 않고 대체 표시.** 삭제하면 `_intake_record.json` crosswalk와
`_source_ledger.json` 참조가 고아가 되고 불변 원본→논리문서 감사 추적이 사라진다.

⚠️ **스키마 충돌**: `downstream_disposition: expert_review_only`는 조건부 검증
(document_manifest.schema.json:134-152)이 "사람이 사진 증거임을 확인했다"는 의미를
강제하므로 **대체된 번들에는 의미가 틀리다.** → **`superseded_bundle` enum 값 추가 권장**
(더 작고 정직하며, 이 필드로 분기하는 소비자가 실제 일어난 일을 뜻하는 값을 받는다).
enum 추가 + 소비자 grep 필요.

신규 엔트리: `DOC_{n:03d}` 최대값 다음부터 채번(번들 id 재사용 금지),
`insert_pdf(from_page, to_page)`(intake_case.py:454-457 패턴),
`file_path`는 Windows에서도 forward slash, `file_size_bytes`는 `save()` **후** stat,
`ocr_status: "pending"`, `pages: null`(owner가 document-pipeline이므로 남겨둔다).

**가드레일 주석 필수**: 분할은 `data/raw/`에 쓴다. 불변인 것은 `source-cases/`이고
`data/raw/`는 intake의 산출물이며 세그먼테이션은 intake의 일부이므로 정당한 writer다.
docstring과 `pipeline.md`에 명시 — 미래의 독자가 위반으로 신고할 바로 그런 종류다.
기존 `data/raw/` 파일은 절대 수정하지 않고 신규 생성만 한다.

**멱등성**: `source_file_name` + `source_page_start` 일치 엔트리가 있으면
`already_split` 보고 후 exit 0.

### H. Halt/Resume 규율

`ocr_extract._resume_cache_dir`(ocr_extract.py:290-315) 패턴 — 실제 75페이지 손실에서
나온 설계라 그대로 따른다.

`_segmentation_scratch/_resume/{case_id}_{doc_id}/`, **pid 태그 없이 안정적**,
시트당 JSON 1개(파싱 결과 + 원문 + provider 메타). 호출 전 캐시 히트면 건너뛰고
`progress()`로 `(cached)` 보고. 저장은 tmp-write-then-`replace()` 원자적 패턴 —
쓰기 중 인터럽트가 resume이 신뢰할 반쪽 시트를 남기지 않게. 손상 JSON → `None` → 재호출.

**시트 이미지 자체도 캐시다.** 110페이지 렌더는 실제 시간이 든다. `_sheets_meta.json`에
기하 해시(crop_ratio, grid, dpi, 페이지 목록)를 저장하고 불일치 시 무효화 —
안 그러면 `--crop-ratio` 변경이 낡은 시트를 조용히 재사용하는 고약하고 거의 안 보이는
버그가 된다.

`split` 실패 시 `run_checkpoint1`의 4단 규율(run_checkpoint1.py:430-450): 포렌식 보존 →
소유 필드 리셋 → run-state `failed` → status dict 반환, `main()`에서만 exit 1.
**신규 documents 리스트를 메모리에서 완성 후 1회 쓰기** — PDF 쓰기가 부분 성공해도
manifest는 전부-아니면-전무. 고아 `DOC_XXX.pdf`는 복구 가능하지만 존재하지 않는 파일을
가리키는 manifest 엔트리는 복구 불가.

### I. 테스트 계획

`tests/test_segment_case.py` 신규. 전부 `tmp_path`, provider는 `FixtureProvider`.

- **기하**: 3×4/4×4/2×4 × crop 0.25/0.33/0.5 전 조합에서 두 상한 준수 단언;
  불가능 요청 시 raise
- **`plan_sheets`**: (110,12)→10장 마지막 2p, (12,12)→정확히 1장(off-by-one), (1,12)→1장
- **파싱**: 정상 / 후행 산문 / 선행 산문 / 파싱 불가→세그먼트 0개+경고+pending이며
  **경계를 발명하지 않음** / 시트에 없는 페이지 번호→파싱 실패 / `needs_full_page` 중복제거
- **세그먼트 검증**: 중첩 거부, 구멍→`unassigned_pages`, 역순 거부, 범위 초과 거부
- **`merge_sheet_proposals`**: 시트 경계를 넘는 문서가 p12에서 잘리지 않고 p1-14로
  이어지는지 — **이 파일에서 가장 가치 있는 테스트**
- **Fallback**: 상한 미만→호출·override / 상한 초과→`saturated`, **provider 호출 0회**
  (호출 횟수 단언), pending 유지
- **스키마**: `ocr_performed: true`인 proposal이 **검증 실패**하는지(const 가드 증명)
- **분할**: 합성 10p PDF(테스트 내 `fitz` 생성)로 3세그먼트→3개 DOC_XXX.pdf,
  페이지 수·provenance 정확, 번들 엔트리 보존+대체 표시, 재실행 멱등
- **Resume**: 캐시 히트 시 provider 미호출(횟수 단언), 손상 캐시 재호출, 기하 불일치 무효화

**실물 E2E** (실제 provider 비용 발생):
```
1) sheets 실행 → PNG 육안 확인: 빨간선·번호 판독 가능한가, 문서 종류 판별 가능한가
   → 여기서 격자 크기 최종 확정 (§4 미확정 사항)
2) 진짜 경계를 손으로 기록 → ground-truth 베이스라인
3) propose --mode vision 실행 → 베이스라인 대비 precision/recall 채점
   needs_full_page 개수 확인, p41류 눕은 페이지가 플래그됐는지 확인
4) approve + split → 결과 DOC_XXX 하나로 run_checkpoint1이 정상 동작하는지
```
**2번 손 베이스라인은 무조건 만든다** — 없으면 crop-ratio나 격자 변경이 도움이 됐는지
악화됐는지 판단할 방법이 없다.

### J. 통합 지점

- **`.claude/agents/document-pipeline.md`** — checkpoint 1 아래에 신규 provenance 필드
  설명 + **`provisional_document_type` 신뢰 금지** 명시(`pre_flagged_type`과 달리
  사람이 단언한 게 아니므로 자체 분류 필수) + 대체된 번들 엔트리는 건너뛴다
- **`pipeline.md:30`** — Stage 1을 "Case Intake & Document Segmentation"으로,
  contact-sheet → proposal → 승인 → 분할 흐름, **"이 단계에서 OCR 없음 — 산출물은
  텍스트가 아니라 문서 구조"** 명시
- **`CLAUDE.md`** — Tools에 `segment_case.py` 추가 + Changelog 항목
- **`.claude/skills/loss-adjustment-pipeline/SKILL.md`** — Stage 1에 Stage 2를 막는
  사람 게이트가 생겼음을 오케스트레이터가 알아야 함
- **`tools/sync_agents.py` 실행** — `.claude/` 수정 후 필수. 생성본 직접 편집 금지
- **`.gitignore`** — `_segmentation_scratch/`
- **`known-gaps.md`** — (1) `intake_case.scan_for_answer_key_content`가 아직 깨진
  라벨 형식을 쓰며 미이관 (2) 눕은 스캔 페이지 클래스
- **`open-decisions.md`** — 격자 크기(§4)와 `downstream_disposition` 충돌(§G)은
  실제 열린 결정이지 확정된 세부가 아니다

---

## 구현 순서

1. ~~`.gitignore` + 신규 스키마 + manifest v0.5; 기존 manifest 회귀 검증~~
   **완료** (`a5f35ef`) — `ocr_performed` const 가드 동작 확인, 7가지 accept/reject
   케이스 검증, 기존 manifest 6개 회귀 없음
2. **← 지금 여기.** 순수 함수 + 테스트 (아직 I/O 없음)
   **범위 (사용자 확정): 순수 함수와 테스트만. 렌더링/합성은 3단계로.**

   `tools/segment_case.py`에 I/O 없는 함수 6개만 작성:
   `compute_sheet_geometry`, `plan_sheets`, `parse_segmentation_response`,
   `validate_segments`, `merge_sheet_proposals`, `build_manifest_entries`

   `tests/test_segment_case.py` 신규 (§I의 기하/plan_sheets/파싱/세그먼트검증/merge
   항목). 나머지 테스트 항목은 해당 코드가 생기는 단계에서.

   **완료 기준**: 새 테스트 전부 통과 + 기존 287개 통과 유지
   (`test_dao_forbidden_expr`의 1건은 merge에서 들어온 기존 실패라 제외)

3. ~~렌더러 + 합성기 → **실제 시트를 눈으로 본다**~~ **완료.** 격자는 4×4로 확정(§4),
   눕은 페이지 감지기가 작동하지 않음을 발견해 투영분산 방식으로 교체(§6).
   `sheets` CLI 연결은 남음
4. 손으로 ground-truth 경계 베이스라인 기록
5. provider 경로(`propose`, Mode B) + resume 캐시 + fallback + FixtureProvider 테스트
6. `approve` + `split` + `dao.replace_manifest_documents` + 테스트
7. 실물 E2E → 4번 대비 채점
8. 문서 → `sync_agents.py` → `pytest`

---

## 열린 결정

**해결됨:**
- ~~`downstream_disposition`~~ → `superseded_bundle` enum 추가로 확정, 1단계에서 구현
  완료. `expert_review_only`를 재사용하지 않은 이유는 그 값의 조건부가
  `non_text_verification`(사람이 사진 증거임을 확인했다는 주장)을 요구하는데, 대체된
  번들에는 그게 거짓이기 때문 — 기록에 허위 사람 검증을 남기게 된다.
- ~~merge 경계 사례 4건~~ → 위 표에서 확정.

**3단계에서 해결됨:** 격자 크기 → 4×4 확정(§4).

**남은 것:**
1. **Fallback 포화 시** — 많이 플래그되면 재튜닝을 위해 중단할지, 비용을 지출할지.
   상황은 나아졌다: 회전 페이지가 주범일 것으로 우려했는데, 실측에서 모델이 그것을
   읽어냈고 16장 중 4장(25%, 상한선)만 full-page로 올렸다. 그 4장 중 2장은 시트
   가장자리 페이지라 **전체 이미지를 봐도 해결되지 않는다** — 모호함의 원인이 해상도가
   아니라 앞 시트에 있기 때문이다. 가장자리 유보를 포화 계산에 넣을지부터 정할 만하다.
2. **Mode A 기본값의 실효성** — 344페이지는 4×4에서 22장이다. 실제 시트를 본 지금은
   한 장을 1분 안에 훑을 수 있어 보이므로 수동 검토가 처음 가정보다 현실적이다.
   전체 케이스로 확인 필요.
