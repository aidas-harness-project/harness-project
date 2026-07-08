---
name: component-output-contract
description: 손해사정 파이프라인 컴포넌트의 출력 JSON을 작성·검증할 때 반드시 사용. outputs/CASE_XXX/에 *_result.json, manifest, 리포트 JSON 등 계약 파일을 쓰거나, 스키마 검증, 공통 필드(case_id·run_id·confidence·evidence_references), manifest 갱신, _workspace 노트 작성이 필요한 모든 상황에서 이 스킬을 따를 것. 파이프라인 산출물을 만들면서 이 스킬 없이 임의 형식으로 쓰는 것은 금지.
---

# 컴포넌트 출력 계약 — 실행 규칙

모든 파이프라인 산출물은 이 계약을 따라야 다음 단계가 신뢰하고 읽을 수
있다. 형식의 정본은 `wiki/templates/component-output.md`와 `schemas/`이며,
이 스킬은 실행 절차를 담는다.

## 1. 출력 작성

모든 출력 JSON에 공통 필드를 넣는다:

```json
{
  "case_id": "CASE_001",
  "run_id": "RUN_20260707_001",
  "component": "ClaimFieldExtraction",
  "status": "success | partial | failed",
  "created_at": "2026-07-07T15:30:00+09:00",
  "model_info": { "model_name": "...", "prompt_version": "..." },
  "confidence": 0.82,
  "review_required": true,
  "reviewer_role": "손해사정사 | 의사 | 법률전문가",
  "evidence_references": [ { "document_id": "DOC_001", "page": 1, "quote": "..." } ],
  "warnings": []
}
```

판단성 출력(추출·분류·생성)에는 안전 필드를 추가한다:
`source_grounded`, `hallucination_risk_check`, `prohibited_language_check`.

지켜야 하는 이유가 있는 규칙들:

- **모든 주장·추출값은 원문 인용과 연결한다.** 연결 불가능하면
  `source_grounded: false` + `review_required: true` — 스키마가 이 조합을
  강제하므로 어기면 검증에서 거부된다. 근거 없는 값을 그럴듯하게 쓰는
  것이 이 PoC의 가장 큰 실패 모드(환각)다.
- **`run_id`는 실행 전체에서 동일하게 쓴다** (오케스트레이터가 발급).
  평가 결과를 특정 실행·프롬프트 버전과 연결하는 열쇠다.
- **`warnings`는 다음 단계를 위한 것이다** — 품질 저하, 자료 부족, 애매한
  판단을 겪었으면 기록한다. 빈 배열은 "특이사항 없음"의 명시적 선언이다.

## 2. manifest 갱신 (document_manifest.json을 만질 때만)

manifest는 여러 단계가 이어 쓰는 공유 파일이다. **자기 owner 필드만
추가하고 남의 필드는 절대 수정하지 않는다** — 필드별 owner는
`schemas/document_manifest.schema.json`의 description에 명시돼 있다.
이 규율이 깨지면 앞 단계의 기록이 소리 없이 사라진다.

## 3. 검증 게이트 (필수)

출력을 다음 단계로 넘기기 전에 반드시 실행한다:

```
python tools/validate_output.py outputs/CASE_XXX/<파일>.json
```

- PASS → 진행. FAIL → 오류 메시지를 보고 **스스로 1회 수정** 후 재검증.
  재실패 → 진행을 멈추고 `_workspace/RUN_XXX/{순서}_{agent}_errors.md`에
  기록 후 오케스트레이터에 보고한다. 검증 안 된 파일을 넘기면 오염이
  하류 전체로 번지므로, 실패 상태로 계속 가는 것보다 멈추는 게 싸다.
- SKIP(스키마 미확정)이 나오면 `wiki/templates/component-output.md`의 공통
  필드 규칙만이라도 지키고, `warnings`에 "스키마 미확정 상태로 생성"을 남긴다.

## 4. _workspace 노트

실행별 폴더 `_workspace/RUN_XXX/`에 `{순서}_{agent}_{artifact}.md` 규약으로
남긴다 (예: `03_claim-analysis_notes.md`). 노트에는 계약 파일에 담기지
않는 것만 쓴다: 애매했던 판단, 다음 단계가 알아야 할 맥락, 에러 상황.
계약 산출물(JSON·md 리포트)을 _workspace에 두지 않는다 — 진실의 원천은
`outputs/`와 `data/processed/` 하나씩이다.

**노트는 먼저 만들고 진행하며 갱신한다 — 종료 직전 몰아쓰기 금지.**
작업을 시작할 때 노트 파일을 먼저 생성(제목·담당 단계·입력만이라도)하고,
판단이 갈리거나 애매한 지점을 만날 때마다 즉시 append한다. 이유: 세션
컴팩트·예산 한도·에러로 에이전트가 중간에 끊겨도 그때까지의 맥락이
디스크에 남아야 오케스트레이터나 다음 세션이 이어받을 수 있다. 노트를
마지막에 한 번에 쓰면 중단 시 통째로 유실된다(RUN_20260707_001에서 실제
발생 — 핵심 맥락이 우연히 출력 JSON의 `warnings`에 있어 인계됐을 뿐이다).
`warnings`에 담기 애매한 과정 맥락일수록 노트에 즉시 남긴다.

## 5. 금지 사항

- `data/ground_truth/`와 `POC/`의 손해사정서·지급내역 접근 —
  critic-evaluation의 평가 단계만 예외. 정답지가 입력에 섞이면 PoC 평가
  전체가 무효가 된다.
- 원자료(`POC/`, `sources/`, `case_qna.pdf`) 수정 — 읽기 전용.
- 단정적 법률·의료 표현 — `wiki/templates/forbidden-expressions.md`의
  대체 표현을 쓴다.
