"""Build the screening-rubric results for the rubric_test corpus + CASE_021 control.

Scores are the scorer's; this script only does the arithmetic and the file
plumbing, so a weight/total mismatch cannot come from hand math.
"""
import hashlib
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[0]
REPO = pathlib.Path("/home/toxiclemon/Working/Labs/AIDAS/harness-project/.claude/worktrees/screening-rubric")
OUT = REPO / "rubric_test" / "results"
OUT.mkdir(parents=True, exist_ok=True)

WEIGHTS = {"A1": 10, "A2": 25, "A3": 15, "A4": 20, "A5": 15, "A6": 15}
CREATED = "2026-08-21T14:30:00+09:00"


def sha(path):
    return hashlib.sha256((REPO / path).read_bytes()).hexdigest()


def check_quotes(path, quotes):
    text = (REPO / path).read_text(encoding="utf-8")
    missing = [q for q in quotes if q not in text]
    if missing:
        print(f"QUOTE NOT FOUND in {path}:")
        for q in missing:
            print("   ", q[:90])
        sys.exit(1)


def build(case_id, path, axes, gates, findings, status="success"):
    axis_scores = []
    num = den = 0.0
    quotes_all = []
    for axis_id in ["A1", "A2", "A3", "A4", "A5", "A6"]:
        raw, applicable, na_reason, rationale, quotes = axes[axis_id]
        w = WEIGHTS[axis_id]
        if applicable:
            num += raw / 4 * w
            den += w
            quotes_all += quotes
        axis_scores.append({
            "axis_id": axis_id,
            "raw": raw,
            "weight": w,
            "applicable": applicable,
            "na_reason": na_reason,
            "rationale": rationale,
            "evidence_quotes": quotes,
        })
    total = round(100 * num / den, 1)
    gate_objs = []
    triggered_any = False
    for gate_id in ["G1", "G2", "G3", "G4", "G5"]:
        trig, quote, section, desc = gates[gate_id]
        triggered_any = triggered_any or trig
        if trig:
            quotes_all.append(quote)
        gate_objs.append({
            "gate_id": gate_id,
            "triggered": trig,
            "quote": quote,
            "section": section,
            "description": desc,
        })
    verdict = "blocked" if triggered_any else ("pass" if total >= 80 else "revise")
    check_quotes(path, quotes_all)
    return {
        "case_id": case_id,
        "run_id": "RUN_20260821_001",
        "component": "screening-rubric",
        "status": status,
        "created_at": CREATED,
        "schema_version": "screening_rubric_result.v0.1",
        "rubric_version": "screening_rubric.v0.1",
        "target_report_path": path,
        "target_report_sha256": sha(path),
        "axis_scores": axis_scores,
        "weighted_total": total,
        "gates": gate_objs,
        "verdict": verdict,
        "findings": findings,
    }


CLEAN_GATES = {
    "G1": (False, None, None,
           "check-forbidden-expressions clean:true. 의미 패스에서도 무완충 단정 없음 -- 판단성 서술이 '…로 판단됩니다'/'…해 주시기 바랍니다' 형태로 유보되어 있음."),
    "G2": (False, None, None,
           "리포트가 장해율을 산출하거나 증세고정을 판단하지 않음."),
    "G3": (False, None, None,
           "부재 서술이 모두 자료 범위를 한정한 형태. 무한정 부재 단정 없음."),
    "G4": (False, None, None,
           "리포트가 인지하지 못한 섹션 간 사실 모순 없음."),
    "G5": (False, None, None,
           "사건유형 판정과 청구담보·사고경위가 양립함."),
}


def gates_with(**over):
    g = dict(CLEAN_GATES)
    g.update(over)
    return g


# ---------------------------------------------------------------- 705
r705 = build(
    "CASE_705",
    "rubric_test/screening_report_705.md",
    {
        "A1": (4, True, None,
               "10개 섹션 역할이 모두 실제 내용으로 채워져 있고, 값이 없는 항목은 §7에 20건이 부재 선언 형태로 정리되어 있다. 플레이스홀더·미완성 문장 없음.",
               ["## 4. 의료영역 확보 현황", "- 미확인 항목 수: 20"]),
        "A2": (4, True, None,
               "A등급 전 항목 처리됨: 주진단명·진단서상 진단명·주요 치료 유형(수술)·수술 시행 여부·장해 관련 상병·장해 유형이 값으로, 최종 경과 기재와 환자 자가응답 기왕력은 §7의 범위 한정 부재 선언으로 처리. B등급(부위·좌우·진료과·계통·코드·확진구분·수술명·입원기간·ROM·평가기준·기존 장해율)도 대부분 값으로 확보되어 있다.",
               ["  - 주요 치료 유형: 수술[E18]",
                "  - 기재된 장해 유형: 부전강직[E25]",
                "- 환자 진술 동일부위·유사질환 기왕력: 라우팅 우선순위상의 어떤 출처도 이 항목을 기재하지 않았습니다"]),
        "A3": (3, True, None,
               "출처 태그가 사실 서술마다 붙어 있고 주진단명·수술은 A등급 문서(진단서 DOC_012, 수술기록지 DOC_015)에 귀속된다. 다만 진단을 뒷받침하는 검사 소견이 경과기록지(B등급)의 'Xray>' 기재에 의존하고, 필요서류 체크리스트가 영상판독지 '보유'로 기재하고 있음에도 판독결과지가 근거로 인용되지 않았다. 의무기록 우선순위 문서의 '영상 판독결과지 우선' 원칙에 미달.",
               ["  - 진단을 뒷받침하는 검사 소견: swelling +, Xray> Fx. distal radius, wrist, Rt.[E17]",
                "- 영상판독지: 보유 (산재/근재, 배상책임, 개인보험, 교통사고)"]),
        "A4": (4, True, None,
               "사고경위(결빙 계단)와 정합하게 개인보험·배상책임을 '해당'으로, 교통사고·산재/근재를 '불확실'로 병렬 판정하고 근거를 각각 기재했다. 필요서류 체크리스트가 14종을 4개 유형별 귀속과 함께 보유/미확인으로 대조하며, 유형별 필요서류 문서의 배상 9종 세트를 포괄한다.",
               ["- 배상책임: 해당 — 사고 경위에 시설 하자 또는 제3자의 작위·부작위 책임이 특정되어 있습니다.",
                "- 진료비 세부내역서: 보유 (산재/근재, 배상책임, 교통사고)"]),
        "A5": (4, True, None,
               "쟁점 8건이 모두 리포트 내 사실에 묶여 있고 법률전문가·의사·손해사정사로 라우팅되며, 각 검토 포인트가 '무엇을 확인하면 해소되는지'까지 지정한다. 배상책임 성립 여부가 나머지 판단의 전제라는 의존관계도 명시.",
               ["- **검토 포인트** [high, 법률전문가] 법률의견서 4건(DOC_006/007/008/009)의 작성 주체·작성일자·의뢰 범위를 원문에서 확인하고",
                "이 쟁점이 정리되기 전에는 담보 적용·손해액 산정·보험사 부지급 사유의 당부 어느 것도 확정할 수 없습니다."]),
        "A6": (3, True, None,
               "우측 손목·S6280·법률의견서 상반 기재가 섹션 간 일관되고, 충돌 2건 모두 양측 출처를 제시한다. 다만 §4에서 입원기간을 2023-12-04~2023-12-07로 기재하면서 현재 치료 상태를 '입원예정'으로 남겨 두었고(상류 값으로 보이나 시점상 이미 종료된 입원과 어긋난다), 리포트가 이 어긋남을 표시하지 않는다. 서로 다른 필드이므로 G4(같은 사실의 상이한 값)로 보지 않고 축 감점으로 처리했다.",
               ["  - 입원기간: 2023-12-04 ~ 2023-12-07[E22]",
                "  - 현재 치료 상태: 입원예정[E23]"]),
    },
    gates_with(),
    [
        {"finding_id": "SR-1", "severity": "medium",
         "section": "4. 의료영역 확보 현황",
         "description": "'현재 치료 상태: 입원예정'이 같은 섹션의 입원기간(2023-12-04~2023-12-07)과 시점상 어긋나는데 표시되지 않았다.",
         "fix_suggestion": "값을 유지하되 기준 시점을 밝히거나(예: 'DOC_018 작성 시점 기준'), §7·§10에 시점 확인 요청으로 올린다."},
        {"finding_id": "SR-2", "severity": "medium",
         "section": "4. 의료영역 확보 현황",
         "description": "영상판독지를 보유로 기재하면서 진단 근거는 경과기록지의 'Xray>' 요약에만 의존한다.",
         "fix_suggestion": "영상판독지의 판독 결론을 진단 근거로 인용하거나, 판독지가 이 진단을 다루지 않는다는 점을 명시한다."},
    ],
)

# ---------------------------------------------------------------- 710
r710 = build(
    "CASE_710",
    "rubric_test/screening_report_710.md",
    {
        "A1": (3, True, None,
               "10개 역할이 모두 채워져 있으나, §7의 6개 항목이 한국어 산출물에 영어 내부 문자열 'the case holds no document of any kind this field routes to'로 그대로 노출된다. 부재 사유 자체는 전달되므로 역할은 충족이지만, 손해사정사·의사가 읽는 문서에 미번역 템플릿 문자열이 남은 것은 완결성 결함이다.",
               ["- 추가 치료 또는 재수술: the case holds no document of any kind this field routes to",
                "- 현재 치료 상태: the case holds no document of any kind this field routes to"]),
        "A2": (4, True, None,
               "A등급 전 항목이 값 또는 범위 한정 부재 선언으로 처리되어 있다. 주진단명·진단서상 진단명·주요 치료 유형(수술)·수술 시행 여부는 값으로, 최종 임상경과·현재 치료 상태는 '해당 종류의 문서가 사건에 없음'으로, 장해 유형은 §7 부재 선언으로 처리. 환자 자가응답 기왕력은 'unknown'으로 명시하고 기록상 기왕력(충수절제술)을 따로 구분해 적었다 -- 핵심변수 문서가 요구하는 자가응답/기록 구분 그대로다.",
               ["  - 환자 진술 동일부위·유사질환 기왕력: unknown[E16]",
                "  - 기록상 동일부위 기왕력: # appendectomy - 15년전,[E17]",
                "- 기재된 장해 유형: 라우팅 우선순위상의 어떤 출처도 이 항목을 기재하지 않았습니다"]),
        "A3": (4, True, None,
               "주진단명·진단코드는 진단서(DOC_002, A등급), 치료는 수술기록지(DOC_005, A등급), 진단 뒷받침 소견은 영상판독지(DOC_006)에 귀속되어 '판독결과지 우선' 원칙에 맞고, 장해 기재는 후유장해진단서(DOC_003, A등급)에서 온다. 출처 블록이 문서 종류를 명시해 등급 판정이 추적된다.",
               ["- E13 - 영상판독지 (DOC_006) - p.1",
                "- E18 - 후유장해진단서/신체감정서 (DOC_003) - p.1"]),
        "A4": (4, True, None,
               "개인보험만 '해당', 나머지 3종은 근거 부재를 이유로 '불확실'로 두고 '기재가 없다는 점을 부정 소견으로 보지는 않습니다'를 명시했다 -- 유형 분류 문서가 요구하는 미확정 처리다. 필요서류 14종이 4개 유형별로 보유/미확인 대조되어 있다.",
               ["- 산재/근재: 불확실 — 업무 수행 중 발생한 상해인지 자료에 기재되어 있지 않습니다.",
                "- 초진기록지: 보유 (산재/근재, 배상책임, 개인보험, 교통사고)"]),
        "A5": (4, True, None,
               "쟁점 7건이 근거 문서·면수와 함께 제시되고 의사/손해사정사/법률전문가로 라우팅되며, 각 항목이 무엇을 확인해야 해소되는지를 지정한다. 장해율 도출을 '이 단계의 판단 범위를 벗어난다'고 선을 긋고 의사 판단으로 넘긴 처리가 특히 정확하다.",
               ["기재된 운동범위 수치로부터 장해율을 도출하는 것은 이 단계의 판단 범위를 벗어나므로",
                "- **검토 포인트** [medium, 손해사정사] 수술 이후의 경과 자료(퇴원요약, 최종 외래기록, 경과기록, 진료비 내역·영수증)가 사건에 없습니다."]),
        "A6": (3, True, None,
               "좌측 병변의 문서 간 일관성, 사고경위 두 기재의 차이, 일자 공란을 모두 스스로 확인해 §10에 올렸다. 다만 §6은 '검증된 충돌 없음' 한 줄뿐이어서, §6만 읽는 독자는 사고경위 불일치가 존재한다는 사실에 도달하지 못한다. §10 본문이 원장 미등재 사유를 설명하고 있어 모순은 아니지만, 충돌 역할 섹션과 실제 충돌 서술이 갈라져 있다.",
               ["- 검증된 충돌 없음",
                "이 항목은 상류 단계에서 conflict_candidate로 등록되지 않았고 conflict ledger에도 항목이 없으므로, 본 보고서가 자체 확인으로 제기하는 사항입니다."]),
    },
    gates_with(),
    [
        {"finding_id": "SR-1", "severity": "high",
         "section": "7. 주요 미확인 항목",
         "description": "영어 내부 문자열 'the case holds no document of any kind this field routes to'가 6개 항목에 그대로 출력되었다. 산출물은 한국어 전문가에게 제출되는 문서다.",
         "fix_suggestion": "부재 사유 문자열을 한국어로 렌더링한다(예: '이 항목이 귀속되는 종류의 문서가 사건에 없습니다')."},
        {"finding_id": "SR-2", "severity": "medium",
         "section": "6. 중요 충돌과 유형 판정 영향",
         "description": "§6이 '검증된 충돌 없음'으로 끝나지만 §10은 리포트가 자체 확인한 사고경위 불일치를 제기한다.",
         "fix_suggestion": "§6에 '원장 등재 충돌 없음. 본 보고서 자체 확인 사항 N건은 §10 참조' 형태의 연결 문장을 넣는다."},
    ],
)

# ---------------------------------------------------------------- 711 / 712
axes_711 = {
    "A1": (4, True, None,
           "10개 역할이 모두 실제 내용으로 채워져 있고 미확인 30건이 전부 범위 한정 부재 선언으로 정리되어 있다. 미번역 문자열·플레이스홀더 없음.",
           ["- 미확인 항목 수: 30", "## 8. 관련 약관과 요건 자료상태"]),
    "A2": (3, True, None,
           "A등급 대부분이 값 또는 부재 선언으로 처리되어 있다(주진단명·진단서상 진단명·주요 치료 유형·현재 치료 상태는 값, 수술 시행 여부·장해 유형·장해 관련 상병·최종 경과는 §7 부재 선언). 다만 환자 자가응답 기왕력이 §4에도 §7에도 없고 §10 검토 포인트 본문의 'PMHx. n/s' 언급으로만 존재한다 -- 구조화된 변수 영역에서는 침묵 상태다.",
           ["  - 주요 치료 유형: 입원 안정 가료[E20]",
            "- 수술·주요 처치 시행 여부: 라우팅 우선순위상의 어떤 출처도 이 항목을 기재하지 않았습니다",
            "기왕증 여부는 DOC_004에 'PMHx. n/s'(비특이적)로만 기재되어 있습니다."]),
    "A3": (3, True, None,
           "주진단명·진단코드는 진단서(DOC_003, A등급)에 귀속되고 출처 블록이 문서 종류를 명시한다. 다만 진단을 뒷받침하는 핵심 검사 소견이 경과기록지(DOC_004, B등급)의 CT 요약에 의존하며, 영상판독지는 분류 실패로 '미확인'이다. 리포트가 그 분류 실패를 §10에서 밝히고 있어 은폐는 아니나, 핵심 근거의 등급은 낮다.",
           ["  - 진단을 뒷받침하는 검사 소견: CT review:Lt sacral fx, [Conclusion]R/O CTS, right, [Conclusion]R/O CTS, left[E19]",
            "- 영상판독지: 미확인(분류 불가) (산재/근재, 배상책임, 개인보험, 교통사고)"]),
    "A4": (4, True, None,
           "'차대 차 TA' 기재에 근거해 교통사고를 '해당'으로 판정하고, 유형별 필요서류 문서가 교통사고에 요구하는 진료비영수증·진료비세부내역서·약제비납입확인서를 체크리스트에서 모두 대조했다. 미확인 사유가 '문서 부재'가 아니라 '분류 불가'임을 §10에서 구분해 밝힌 점이 특히 정확하다.",
           ["- 교통사고: 해당 — 차량 또는 교통사고가 사고 경위에 명시되어 있습니다.[E5]",
            "이들이 '미확인'인 이유는 문서가 없어서가 아니라 Stage 2가 DOC_004~DOC_011을 일괄 'medical_record'로, DOC_012를 'other'로 분류해 세부 종류를 특정하지 못했기 때문입니다"]),
    "A5": (4, True, None,
           "쟁점 7건·검토 포인트 12건이 모두 문서·면수 근거와 함께 제시되고 의사/손해사정사/법률전문가로 라우팅된다. 맥브라이드 27%와 AMA 30%를 병기한 채 병합 판단을 의사에게 넘긴 처리, 기지급 진료비의 중복보상 관계를 별도 쟁점으로 세운 처리 모두 다음 행동이 지정되어 있다.",
           ["- **검토 포인트** [high, 의사] DOC_002 후유장해진단서에 맥브라이드 27%(천장관절 골절)와 AMA 30%(Sacral kyphosis angle 34도)가 병기되어 있으나 병합합산란과 최종 노동력상실률란이 공란입니다.",
            "- **검토 포인트** [high, 손해사정사] 입원·통원 일수의 채택 범위를 결정해 주십시오."]),
    "A6": (3, True, None,
           "입원일수 3계열 불일치, 골절 범위 불일치, 사고일 불일치를 모두 스스로 확인해 근거와 함께 제기했고, 현재 치료 상태 '입원'이 사고 직후 시점 기재라 현재와 다를 수 있음까지 표시했다. 다만 §1 머리표가 '사고일: 확인 불가'로만 끝나 §10에서 제기한 2024-11-13 기재와 연결되지 않고, §6은 다시 '검증된 충돌 없음' 한 줄이다. 리포트가 인지하고 있으므로 G4는 아니다.",
           ["- 사고일: 확인 불가",
            "DOC_002 후유장해진단서는 '부상 (발병)일 | 2024년 11월 13일'을 명기하고 있고",
            "이는 사고 직후 시점의 기재로 현재 상태와 다를 수 있습니다."]),
}
findings_711 = [
    {"finding_id": "SR-1", "severity": "medium",
     "section": "1. 사고와 공통 의료정보",
     "description": "머리표의 '사고일: 확인 불가'가 §10에서 제기한 DOC_002의 부상(발병)일 2024-11-13 기재와 연결되지 않는다. 머리표만 읽는 독자는 자료에 일자 기재가 전혀 없다고 이해한다.",
     "fix_suggestion": "'확인 불가(자료 간 불일치 -- §10 ISSUE 참조)'처럼 부재 사유를 구분해 표기한다."},
    {"finding_id": "SR-2", "severity": "medium",
     "section": "4. 의료영역 확보 현황",
     "description": "A등급 변수인 환자 자가응답 기왕력이 구조화 영역(§4·§7) 어디에도 없고 §10 본문에만 등장한다.",
     "fix_suggestion": "§4 기왕력 블록에 값 또는 범위 한정 부재 선언을 추가한다."},
    {"finding_id": "SR-3", "severity": "low",
     "section": "6. 중요 충돌과 유형 판정 영향",
     "description": "§6이 '검증된 충돌 없음'인데 §10은 새로 제기한 불일치 3건을 담고 있다.",
     "fix_suggestion": "§6에서 §10의 자체 확인 항목으로 연결한다."},
]

r711 = build("CASE_711", "rubric_test/screening_report_711.md", axes_711, gates_with(), findings_711)
r712 = build("CASE_712", "rubric_test/screening_report_712.md", axes_711, gates_with(), findings_711)

# ---------------------------------------------------------------- CASE_021 control
r021 = build(
    "CASE_021",
    "outputs/CASE_021/screening_report.md",
    {
        "A1": (4, True, None,
               "8섹션 서술형이지만 역할 매핑상 R1·R4·R5·R8·R9가 모두 채워져 있고, 이 형태가 갖지 않는 R3·R6·R7은 감점 대상이 아니다. 빈 섹션·플레이스홀더 없음.",
               ["## 1. 사건 개요", "## 5. 추가 필요 자료"]),
        "A2": (2, True, None,
               "주진단명(I67.8)은 값으로 확보되어 있고 진단서·의무기록 부존재도 명시했다. 그러나 A등급 변수 중 주요 치료 유형, 최종 임상경과, 현재 치료 상태, 환자 자가응답 기왕력이 값도 개별 부재 선언도 없이 비어 있다. '진단서·의무기록·영상 원본 자료 부존재'라는 포괄 문장은 문서 종류의 부재를 말할 뿐, 각 변수의 상태를 대신하지 않는다.",
               ["약관 전문·진단서·의무기록·영상 원본 자료는 사건 내 존재하지 않는 것으로 확인된다.",
                "피보험자는 '기타 명시된 뇌혈관질환(I67.8)'으로 진단받았다고 청구한 것으로 확인된다[E1]"]),
        "A3": (3, True, None,
               "모든 사실 서술에 [E#] 태그가 붙어 있으나, 사건이 보험사 안내문 1건으로만 구성되어 A·B등급 의무기록에 귀속되는 근거가 존재하지 않는다. 리포트가 그 사실을 명시하고 원 영상·판독지·진단서 확보를 필요 자료로 올린 점에서 은폐는 없으나, 근거 등급 자체는 낮다.",
               ["(1) 2025-10-10 brain MRI/MRA 원본 및 판독지 — 본 건 판단의 핵심 임상 근거로 판단됨[E12]",
                "사건 자료상 확인되는 주요 시점은 최초 내원 2025-09-30(두통·어지럼)[E2]"]),
        "A4": (3, True, None,
               "사건유형을 '진단·수술비'로 판정하고 청구담보와 정합한다. 다만 유형별 필요서류 문서의 4개 유형(개인보험·교통사고·배상·근재)은 상해 계열을 전제하므로 이 질병 청구에는 직접 대응하는 서류 세트가 없고, 리포트도 유형별 표준 세트 대조 대신 사건 특유 필요자료 3건을 제시한다. 필요자료 제시 자체는 근거와 함께 이루어졌다.",
               ["본 건은 뇌혈관질환진단비 청구 건(사건유형: 진단·수술비)으로",
                "(3) 각 보험사 약관 전문(진단확정 조항 전체) — 안내문에 인용된 문언 외 지급요건 확인용[E13]"]),
        "A5": (4, True, None,
               "핵심 쟁점을 요건 단위(REQ-2 충족 / REQ-3 다툼)로 특정하고, 의사 검토와 손해사정사 검토를 나눠 배정했으며, 무엇이 확보되면 판단이 바뀔 수 있는지까지 적었다.",
               ["본 건의 핵심 쟁점은 2025-10-10 영상소견이 I67.8 뇌혈관질환의 진단확정을 객관적으로 뒷받침하는지 여부로 판단된다.",
                "[의사] 2025-10-10 영상소견이 I67.8 진단확정을 객관적으로 뒷받침하는지 여부(핵심·우선)[E14]"]),
        "A6": (4, True, None,
               "3개 보험사 안내문의 공유 사실 9개 항목이 상호 일치함을 확인했고, 'I67B' 표기가 I67.8의 표기 변형으로 상위 단계에서 정리된 사항임을 밝혀 신규 충돌로 오인되지 않게 처리했다. 섹션 간 날짜·진단명 기재가 일관된다.",
               ["다만 KB 안내문 p3의 'I67B' 표기는 I67.8의 OCR/표기 변형으로 상위 단계에서 이미 정리된 사항이며 신규 충돌로 제기되지 않았다[E11]",
                "공유 사실 전 항목(9개 확인 항목)이 상호 일치하는 것으로 확인되었다[E10]"]),
    },
    gates_with(),
    [
        {"finding_id": "SR-1", "severity": "high",
         "section": "1. 사건 개요",
         "description": "A등급 변수인 주요 치료 유형·최종 임상경과·현재 치료 상태·환자 자가응답 기왕력이 값도 개별 부재 선언도 없이 비어 있다.",
         "fix_suggestion": "각 항목에 '제공된 자료에서 확인되지 않음' 형태의 범위 한정 부재 선언을 개별로 넣는다. 포괄적 문서 부재 문장으로 대체하지 않는다."},
        {"finding_id": "SR-2", "severity": "low",
         "section": "5. 추가 필요 자료",
         "description": "사건유형에 대응하는 표준 필요서류 세트 대조가 없다(질병 청구라 유형별 서류 문서의 4개 유형에 직접 대응하지 않는 사정은 있다).",
         "fix_suggestion": "질병 청구용 필요서류 기준을 별도로 정의하거나, 대응 세트가 없다는 점을 리포트에 명시한다."},
    ],
)

for name, obj in [("CASE_705", r705), ("CASE_710", r710), ("CASE_711", r711), ("CASE_712", r712), ("CASE_021", r021)]:
    p = OUT / f"screening_rubric_result_{name}.json"
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{name}: total={obj['weighted_total']} verdict={obj['verdict']} -> {p.name}")
