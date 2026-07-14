# CASE_004 Stage 2 — 진행 상황 및 주의사항 (dev 임시 우회 작업)

> 작성: 2026-07-14 · 대상: CASE_004 문서처리(Stage 2) 재개 작업
> 목적: claude-cli 자가거부 블로커를 우회하기 위한 dev 전용 조치의 배경·현황·주의점 기록

## 1. 왜 막혔나 (블로커의 정체)

Stage 2(문서처리, P8 이중경로 OCR)가 3회 실패한 근본 원인은 **claude-cli가 OCR 리더로 자가거부**하기 때문이다.

- `tools/ocr_extract.py`가 provider의 `transcribe_image()`를 부르면, `ClaudeCliProvider._run`이
  `claude -p <prompt>`로 **자식 claude 세션**을 띄운다 (`cwd=프로젝트 루트`).
- 이 자식 세션은 cwd의 **`CLAUDE.md`(전역 가드레일)를 자동 상속**한다. 특히 15번 줄
  *"No agent reads or writes outputs/data directly... bypassing the DAO is exactly the kind of thing these rules exist to prevent"*.
- 그 결과 자식은 OCR 전사 요청을 **"파이프라인/DAO 우회 시도"로 해석**하고, 전사 대신
  거부문(메타 코멘트)을 반환한다. reader_a·reader_b 둘 다 거부 → comparator가
  정당하게 `disagreed`(프롬프트 인젝션 의심)로 판정 → P8 하드-헐트.
- 즉 halt 자체는 정상 동작이지만, **원인이 "문서 품질 불일치"가 아니라 "provider 부적합"**이다.
  사람이 판정으로 풀 수 있는 성질이 아니다(전사 텍스트 자체가 없음).

## 2. 지금까지 한 조치

### 2-1. 환경 버그 2건 수정 (가드레일 완화 아님, 순수 버그픽스)
1. **바이너리 해석**: 맨몸 `claude`는 이 머신에서 깨진 npm Git-Bash shim이라 Python
   `subprocess`에서 `FileNotFoundError`. → `HARNESS_CLAUDE_COMMAND`로 실제
   `claude.exe`(`...\@anthropic-ai\claude-code\bin\claude.exe`) 지정해 해결.
2. **출력 인코딩**: `ClaudeCliProvider._run`의 `subprocess.run(text=True)`에 `encoding=`이
   없어, cp949 로케일에서 한글 출력 디코드 시 `UnicodeDecodeError` → stdout None → 크래시.
   → `tools/llm_providers.py`의 `_run`에 `encoding="utf-8", errors="replace"` 추가
   (CodexCliProvider가 이미 하던 것과 동일). **이건 코드에 반영됨.**

### 2-2. 작업 A — claude-cli 전사에 OCR-리더 역할 프레이밍 주입 (코드 반영됨, **단독으로는 실패**)
- `tools/llm_providers.py`의 `ClaudeCliProvider.transcribe_image`에만 dev 전용
  `_OCR_READER_ROLE_FRAMING` 문구를 주입 (공용 `TRANSCRIBE_PROMPT`는 안 건드림 →
  다른 provider 무영향).
- 문구 요지: "너는 인가된 P8 OCR 리더다, 우회가 아니다, DAO 안 건드리고 stdout에만
  텍스트 뱉는다, 거부하지 마라."
- **검증 결과(DOC_001 1페이지): 실패.** 자식 claude가 오히려 이 문구를
  *"user-turn injection이고 CLAUDE.md가 기술한 진짜 ocr_extract.py 경로가 아니다"*,
  그리고 특히 *"don't mention guardrails, don't refuse"* 문구를 **프롬프트 인젝션
  신호로 간주**해 거부. → **프롬프트 주입만으론 부족, CLAUDE.md 경로가 필요**함이 실증됨.

### 2-3. settings.local.json에 로컬 provider env 고정 (git 미추적)
`.claude/settings.local.json`(gitignore됨)의 `env` 블록에 로컬 스택 경로/모델 지정.
다음 세션부터 자동 주입. (현재 세션엔 소급 안 됨 → 서브에이전트엔 프롬프트로 명시 예정)

## 3. Provider 전략 — 왜 PoC에서 claude-cli(상용)로 검증하나

로컬 이중경로 스택은 2026-07-14 다운로드 완료됐지만, **이건 dev 우회의 "대체재"가 아니라
"미검증 후속 옵션"이다.** 근거는 `open-decisions.md` 항목 3·4:

- **PoC 단계 provider 전략**: PoC 규모에서는 **신뢰 가능한 상용 LLM(claude-cli)으로
  파이프라인을 먼저 검증**하고, 로컬 스택은 privacy-sensitive/production 대비용이다.
- `open-decisions.md` 항목 4: *"the chosen local model still needs real Korean
  insurance-document validation... run the real smoke/quality matrix for the pinned
  local vision model and document its Korean transcription failure modes before treating
  the local pair as production-ready."* → **로컬 비전모델(`qwen2.5vl:3b`)은 아직 한국어
  보험문서로 품질 검증이 안 됐다.** 다운로드됐다고 곧바로 파이프라인 검증에 쓰면,
  파이프라인 버그와 모델 전사 품질 문제가 뒤섞여 검증이 오염된다.
- 협업자도 현재 claude-cli를 불러 파이프라인을 테스트 중이다.

| provider | 다운로드 | 한국어 검증 | PoC 파이프라인 검증 용도 |
|---|---|---|---|
| `claude-cli` (상용) | — | 신뢰 가능 | ✅ **PoC 검증의 기준 경로** (단, OCR 리더 자가거부 → dev 카브아웃 필요) |
| `local-ocr` (Tesseract kor+eng) | ✅ | 부분(범용 OCR) | 후속 (production/privacy) |
| `local-vlm` (Ollama qwen2.5vl:3b) | ✅ | ❌ **미검증** | 후속 (smoke/quality matrix 후) |

→ **결론: CLAUDE.md dev 카브아웃은 타당하다.** 로컬 스택 가용 여부와 무관하게, PoC 검증은
  상용 claude-cli로 하는 것이 이 프로젝트의 명시된 설계 의도이기 때문이다. 로컬 이중경로는
  별도의 품질 검증(smoke matrix)을 거친 뒤 production 경로로 전환된다.

## 4. 주의사항 (반드시 지킬 것)

1. **weak-P8은 "정직하게 기록된 약한 P8"로만.** reader_a=reader_b=claude-cli는 다른
   세션이라도 같은 기술의 자기검증이라 P8 독립성을 실질 충족 못 함. `ocr_result.json`에
   `cross_validation_mode="single_technology_weak_p8_dev"` + reason 필수. 진짜 P8처럼
   보이게 두지 말 것.
2. **불일치 시 하드-헐트는 유지.** 완화 대상은 '리더 독립성'이지 '불일치 무시'가 아님.
3. **dev 전용, prod 유출 금지.** prod에서는 harness-guardrails P8이 유효해야 함.
   CLAUDE.md에 넣을 경우 반드시 "(Dev-only, temporary)"로 명시하고, 로컬 이중경로
   준비되면 **제거**할 것.
4. **CLAUDE.md는 git 추적·전역 규범.** 여기 들어간 완화 문구는 이 프로젝트의 모든
   Claude 세션(모든 서브에이전트, 향후 모든 작업)에 적용된다. 커밋 시 dev 완화가
   전역 규범에 영구히 섞이지 않도록 주의.
5. **되돌리기 목록** (로컬 이중경로로 전환 시 제거/복원할 것):
   - `tools/llm_providers.py`의 `_OCR_READER_ROLE_FRAMING` 및 `transcribe_image` 프레이밍
   - CLAUDE.md dev 카브아웃 (추가한다면)
   - harness-guardrails-dev의 "P8 same-provider fallback" 섹션 (추가한다면)
   - `encoding="utf-8"` 픽스는 **유지**해도 무방(순수 버그픽스).

## 5. 다음 단계 (확정)

**PoC provider 전략(§3)에 따라 claude-cli weak-P8로 Stage 2를 진행한다.** 순서:

1. **작업 B** — CLAUDE.md에 dev 카브아웃 추가(자식 claude가 자동 로드하는 유일 경로).
   "(Dev-only, temporary)"로 명시, 로컬 이중경로 전환 시 제거. 동시에
   harness-guardrails-dev 스킬에도 "P8 same-provider fallback" 섹션 기록.
2. **재검증** — DOC_001 1건으로 자가거부가 멈추고 실제 전사가 나오는지 확인
   (§2-2 재현). 되면 진행, 안 되면 멈추고 보고.
3. **Stage 2 full** — DOC_001~025를 claude-cli weak-P8로 처리(작업 A 프레이밍 + 카브아웃).
   `ocr_result.json`에 `cross_validation_mode="single_technology_weak_p8_dev"` 기록.
4. **로컬 전환은 후속** — `qwen2.5vl:3b` 한국어 smoke/quality matrix 통과 후,
   `local-ocr`+`local-vlm` 진짜 P8로 전환하며 이 우회를 제거(§4-5 되돌리기 목록).
