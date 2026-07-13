// Hardcoded snapshot for the offline/static build -- see api.js.
// Keyed by case_id; CASE_001 is grounded in real (raw-only) source-cases content,
// assembled directly by reading the documents -- no agents/guardrails/DAO were run.
export const STATIC_CASES = {
  "CASE_001": {
    "caseId": "CASE_001",
    "runState": {
      "case_id": "CASE_001",
      "run_id": "RUN_20260710_MOCKUP",
      "created_at": "2026-07-10T15:00:00+09:00",
      "updated_at": "2026-07-10T15:05:00+09:00",
      "stages": [
        {
          "stage_name": "case-intake",
          "status": "passed",
          "started_at": null,
          "completed_at": "2026-07-10T15:05:00+09:00",
          "attempt_count": 0,
          "backup_path": null
        },
        {
          "stage_name": "document-pipeline",
          "status": "passed",
          "started_at": null,
          "completed_at": "2026-07-10T15:05:00+09:00",
          "attempt_count": 0,
          "backup_path": null
        },
        {
          "stage_name": "policy-pipeline",
          "status": "passed",
          "started_at": null,
          "completed_at": "2026-07-10T15:05:00+09:00",
          "attempt_count": 0,
          "backup_path": null
        },
        {
          "stage_name": "claim-analysis",
          "status": "passed",
          "started_at": null,
          "completed_at": "2026-07-10T15:05:00+09:00",
          "attempt_count": 0,
          "backup_path": null
        },
        {
          "stage_name": "consistency-check",
          "status": "passed",
          "started_at": null,
          "completed_at": "2026-07-10T15:05:00+09:00",
          "attempt_count": 0,
          "backup_path": null
        },
        {
          "stage_name": "screening-report",
          "status": "passed",
          "started_at": null,
          "completed_at": "2026-07-10T15:05:00+09:00",
          "attempt_count": 0,
          "backup_path": null
        },
        {
          "stage_name": "draft-report-v1",
          "status": "passed",
          "started_at": null,
          "completed_at": "2026-07-10T15:05:00+09:00",
          "attempt_count": 0,
          "backup_path": null
        },
        {
          "stage_name": "critic-v1",
          "status": "passed",
          "started_at": null,
          "completed_at": "2026-07-10T15:05:00+09:00",
          "attempt_count": 0,
          "backup_path": null
        },
        {
          "stage_name": "evaluation-v1",
          "status": "pending",
          "started_at": null,
          "completed_at": null,
          "attempt_count": 0,
          "backup_path": null
        }
      ],
      "human_input_status": [
        {
          "stage_name": "evaluation-v1",
          "status": "waiting",
          "description": "awaiting expert review before evaluation may access ground truth (D1)",
          "requested_at": "2026-07-10T15:05:00+09:00",
          "received_at": null
        }
      ]
    },
    "ledgers": {
      "source_ledger": {
        "case_id": "CASE_001",
        "source_dir": "source-cases/후유장해 케이스",
        "created_at": "2026-07-10T15:00:00+09:00",
        "updated_at": "2026-07-10T15:00:30+09:00",
        "files": [
          {
            "file_name": "배상-상완골 근위부 골절OP (김태윤) - 고객정보 삭제.pdf",
            "classification": "raw",
            "review_status": "approved",
            "reviewed_by": "김태윤",
            "reviewed_at": "2026-07-10T15:00:30+09:00",
            "rejection_reason": null
          },
          {
            "file_name": "배상 한화손보 손해사정서 - 고객정보 삭제.pdf",
            "classification": "ground_truth",
            "review_status": "approved",
            "reviewed_by": "김태윤",
            "reviewed_at": "2026-07-10T15:00:30+09:00",
            "rejection_reason": null
          }
        ]
      },
      "conflict_ledger": {
        "case_id": "CASE_001",
        "created_at": "2026-07-10T15:00:00+09:00",
        "updated_at": "2026-07-10T15:00:00+09:00",
        "conflicts": []
      }
    },
    "contracts": {
      "extracted_claim_fields.json": {
        "case_id": "CASE_001",
        "component": "claim-analysis",
        "status": "success",
        "confidence": 0.9,
        "review_required": true,
        "reviewer_role": "의사",
        "fields": {
          "diagnosis_name": {
            "value": "상완골 대거친면의 골절, 폐쇄성",
            "normalized_value": "fracture_greater_tuberosity_humerus_closed",
            "confidence": 0.95,
            "review_required": false,
            "is_primary": true,
            "evidence_references": [
              {
                "document_id": "DOC_001",
                "page": 14,
                "quote": "(주) Fracture of greater tuberosity of humerus, closed(상완골 대거친면의 골절, 폐쇄성) [S42.250]"
              }
            ]
          },
          "kcd_code": {
            "value": "S42.250",
            "confidence": 0.95,
            "review_required": false,
            "evidence_references": [
              {
                "document_id": "DOC_001",
                "page": 14,
                "quote": "[S42.250]"
              }
            ]
          },
          "accident_date": {
            "value": "2025-10-16",
            "confidence": 0.85,
            "review_required": false,
            "evidence_references": [
              {
                "document_id": "DOC_001",
                "page": 18,
                "quote": "길 걸어가다 튀어나온 보도블록에 걸려 넘어진후 상기 증상 있어 리드힐정형외과 내원"
              }
            ]
          },
          "surgery_name": {
            "value": "Open reduction and internal fixation, humerus (Rt)",
            "confidence": 0.92,
            "review_required": false,
            "evidence_references": [
              {
                "document_id": "DOC_001",
                "page": 21,
                "quote": "수술명: Open reduction and internal fixation, humerus (부위: right)"
              }
            ]
          },
          "hospital_name": {
            "value": "리드힐정형외과",
            "confidence": 0.8,
            "review_required": false,
            "evidence_references": [
              {
                "document_id": "DOC_001",
                "page": 18,
                "quote": "리드힐정형외과 내원하여 시행한 x-ray상 humerus fx 확인되어 본원 내원"
              }
            ]
          },
          "admission_period": {
            "start_date": "2025-10-16",
            "end_date": "2025-10-23",
            "days": 7,
            "confidence": 0.85,
            "review_required": false,
            "evidence_references": [
              {
                "document_id": "DOC_001",
                "page": 17,
                "quote": "재원일수 7"
              }
            ]
          },
          "disability_rating": {
            "value": "맥브라이드 II-A-4, 18% 영구장해",
            "confidence": 0.9,
            "review_required": true,
            "reviewer_role": "의사",
            "evidence_references": [
              {
                "document_id": "DOC_001",
                "page": 16,
                "quote": "맥브라이드 II-A-4 18% 영구장해에 해당됨."
              }
            ]
          },
          "secondary_diagnosis": {
            "value": "공황장애, 상세불명의 불안장애",
            "normalized_value": "panic_disorder_unspecified_anxiety",
            "confidence": 0.7,
            "review_required": true,
            "reviewer_role": "의사",
            "evidence_references": [
              {
                "document_id": "DOC_001",
                "page": 15,
                "quote": "공황 장애 / 상세불명의 불안장애 (한국질병분류번호 F410, F419)"
              }
            ]
          }
        }
      },
      "case_type_result.json": {
        "case_id": "CASE_001",
        "component": "claim-analysis",
        "status": "success",
        "confidence": 0.88,
        "review_required": false,
        "case_type": "후유장해",
        "template_id": "permanent_disability_v1",
        "evidence_references": [
          {
            "document_id": "DOC_001",
            "page": 16,
            "quote": "맥브라이드 II-A-4 18% 영구장해에 해당됨."
          }
        ]
      },
      "coverage_result.json": {
        "case_id": "CASE_001",
        "component": "claim-analysis",
        "status": "success",
        "confidence": 0.85,
        "review_required": false,
        "coverages": [
          {
            "coverage_type": "배상책임(영조물배상)",
            "confidence": 0.85,
            "evidence_references": [
              {
                "document_id": "DOC_001",
                "page": 5,
                "quote": "본 사고는 보도블록에 걸려 넘어져 발생한 사고로써... 민법 제758조"
              }
            ]
          }
        ]
      },
      "requirement_matching_result.json": {
        "case_id": "CASE_001",
        "component": "claim-analysis",
        "status": "success",
        "confidence": 0.8,
        "review_required": true,
        "reviewer_role": "손해사정사",
        "matches": [
          {
            "requirement": "공작물 설치·보존상 하자로 인한 배상책임",
            "policy_basis": "민법 제758조",
            "satisfied": "검토 필요",
            "evidence_references": [
              {
                "document_id": "DOC_001",
                "page": 5,
                "quote": "공작물의 설치 또는 보존의 하자로 인하여 타인에게 손해를 가한 때에는 공작물점유자가 손해를 배상할 책임이 있다"
              }
            ]
          }
        ]
      },
      "evidence_validation_result.json": {
        "case_id": "CASE_001",
        "component": "consistency-check",
        "status": "success",
        "confidence": 0.9,
        "review_required": false,
        "inconsistencies": []
      },
      "critic_result.json": {
        "case_id": "CASE_001",
        "component": "critic",
        "status": "success",
        "confidence": 0.9,
        "review_required": false,
        "prohibited_language_check": {
          "passed": true,
          "issues": []
        },
        "findings": [
          "모든 인용이 원문과 대조 확인됨.",
          "단정적 법률/의료 표현 없음."
        ]
      },
      "evaluation_result.json": {
        "case_id": "CASE_001",
        "component": "evaluation",
        "status": "partial",
        "confidence": null,
        "review_required": true,
        "reviewer_role": "손해사정사",
        "note": "전문가 검수 대기 중 -- expert_review.json 미제출로 평가 보류 (P7)."
      }
    },
    "reports": {
      "draft_report_v1.md": {
        "markdown": "## 1. 사건 개요\n\n피보험자는 2025년 10월 16일 보행 중 튀어나온 보도블록에 걸려 넘어지는 사고로 상완골 대거친면의 골절(폐쇄성) 진단을 받았다 [E1]. 사고 이후 리드힐정형외과에 내원하여 관혈적 골정복술 및 금속판 고정술을 시행받았다 [E2].\n\n## 2. 후유장해\n\n후유장해진단서상 맥브라이드 II-A-4, 18% 영구장해로 평가되었다 [E3]. 우측 어깨의 운동범위 제한이 확인된다 [E4].\n\n## 3. 쟁점\n\n사고 후 발생한 공황장애 및 불안장애가 본 상해와의 인과관계를 갖는지 검토가 필요하다 [E5]. 상담치료사 소견상 우측 어깨 기능 제한으로 그림 작업(직업 활동)이 중단된 것으로 기재되어 있다 [E6].\n",
        "evidence": {
          "document_path": "draft_report_v1.md",
          "citations": [
            {
              "tag": "E1",
              "document_id": "DOC_001",
              "page": 18,
              "quote": "길 걸어가다 튀어나온 보도블록에 걸려 넘어진후 상기 증상"
            },
            {
              "tag": "E2",
              "document_id": "DOC_001",
              "page": 21,
              "quote": "수술명: Open reduction and internal fixation, humerus"
            },
            {
              "tag": "E3",
              "document_id": "DOC_001",
              "page": 16,
              "quote": "맥브라이드 II-A-4 18% 영구장해에 해당됨."
            },
            {
              "tag": "E4",
              "document_id": "DOC_001",
              "page": 16,
              "quote": "전상방 거상 90(150) 후방거상 30(40) 측상방 거상 80(150)"
            },
            {
              "tag": "E5",
              "document_id": "DOC_001",
              "page": 15,
              "quote": "낙상 사고 이후 발생한 심한 불안 공황 우울 불면 증세를 주소로... 치료중에 있음"
            },
            {
              "tag": "E6",
              "document_id": "DOC_001",
              "page": 25,
              "quote": "우측 어깨 기능 제한으로 인해 그림 작업 수행이 어려워지면서 직업 활동이 중단된 상태임"
            }
          ]
        }
      }
    }
  },
  "CASE_DEMO": {
    "caseId": "CASE_DEMO",
    "runState": {
      "case_id": "CASE_DEMO",
      "run_id": "RUN_DEMO_001",
      "created_at": "2026-07-10T12:23:39.335129+09:00",
      "updated_at": "2026-07-10T12:23:39.677693+09:00",
      "stages": [
        {
          "stage_name": "case-intake",
          "status": "passed",
          "started_at": null,
          "completed_at": "2026-07-10T12:23:39.335144+09:00",
          "attempt_count": 0,
          "backup_path": null
        },
        {
          "stage_name": "document-pipeline",
          "status": "passed",
          "started_at": null,
          "completed_at": "2026-07-10T12:23:39.418278+09:00",
          "attempt_count": 0,
          "backup_path": null
        },
        {
          "stage_name": "policy-pipeline",
          "status": "passed",
          "started_at": null,
          "completed_at": "2026-07-10T12:23:39.508098+09:00",
          "attempt_count": 0,
          "backup_path": null
        },
        {
          "stage_name": "claim-analysis",
          "status": "passed",
          "started_at": null,
          "completed_at": "2026-07-10T12:23:39.591398+09:00",
          "attempt_count": 0,
          "backup_path": null
        },
        {
          "stage_name": "consistency-check",
          "status": "in_progress",
          "started_at": "2026-07-10T12:23:39.677682+09:00",
          "completed_at": null,
          "attempt_count": 1,
          "backup_path": null
        }
      ],
      "human_input_status": []
    },
    "ledgers": {
      "source_ledger": {
        "case_id": "CASE_DEMO",
        "source_dir": "/tmp/demo_case",
        "created_at": "2026-07-10T12:23:38.998289+09:00",
        "updated_at": "2026-07-10T12:23:39.254340+09:00",
        "files": [
          {
            "file_name": "손해사정서.pdf",
            "classification": "ground_truth",
            "review_status": "approved",
            "reviewed_by": "김태윤",
            "reviewed_at": "2026-07-10T12:23:39.254331+09:00",
            "rejection_reason": null
          },
          {
            "file_name": "의무기록.pdf",
            "classification": "raw",
            "review_status": "approved",
            "reviewed_by": "김태윤",
            "reviewed_at": "2026-07-10T12:23:39.170647+09:00",
            "rejection_reason": null
          },
          {
            "file_name": "진단서.pdf",
            "classification": "raw",
            "review_status": "approved",
            "reviewed_by": "김태윤",
            "reviewed_at": "2026-07-10T12:23:39.087440+09:00",
            "rejection_reason": null
          }
        ]
      },
      "conflict_ledger": {
        "case_id": "CASE_DEMO",
        "created_at": "2026-07-10T12:23:39.764093+09:00",
        "updated_at": "2026-07-10T12:23:39.764170+09:00",
        "conflicts": [
          {
            "conflict_id": "CONFLICT_1",
            "raised_by_stage": "consistency-check",
            "field_or_topic": "accident_date",
            "sources": [
              {
                "document_id": "DOC_001",
                "page": 1,
                "value": "2024-03-12",
                "quote": "사고일자 2024년 3월 12일"
              },
              {
                "document_id": "DOC_002",
                "page": 3,
                "value": "2024-03-15",
                "quote": "내원일 2024-03-15"
              }
            ],
            "verdict": "pending",
            "resolution_note": null,
            "resolved_at": null
          }
        ]
      }
    },
    "contracts": {
      "extracted_claim_fields.json": {
        "case_id": "CASE_DEMO",
        "component": "claim-analysis",
        "status": "success",
        "confidence": 0.9,
        "review_required": true,
        "reviewer_role": "손해사정사",
        "fields": {
          "diagnosis_name": {
            "value": "요추 압박골절",
            "confidence": 0.95,
            "review_required": false,
            "evidence_references": [
              {
                "document_id": "DOC_001",
                "page": 1,
                "quote": "요추 압박골절 소견"
              }
            ]
          },
          "kcd_code": {
            "value": "S32.0",
            "confidence": 0.92,
            "review_required": false,
            "evidence_references": [
              {
                "document_id": "DOC_001",
                "page": 1,
                "quote": "S32.0"
              }
            ]
          },
          "accident_date": {
            "value": "2024-03-12",
            "confidence": 0.6,
            "review_required": true,
            "is_primary": true,
            "evidence_references": [
              {
                "document_id": "DOC_001",
                "page": 1,
                "quote": "사고일자 2024년 3월 12일"
              }
            ]
          },
          "hospital_name": {
            "value": "서울정형외과",
            "confidence": 0.88,
            "review_required": false,
            "evidence_references": [
              {
                "document_id": "DOC_001",
                "page": 1,
                "quote": "서울정형외과의원"
              }
            ]
          }
        }
      }
    },
    "reports": {
      "draft_report_v1.md": {
        "markdown": "## 1. 사건 개요\n\n피보험자는 2024년 3월 12일 낙상 사고로 요추 압박골절 진단을 받았다 [E1]. 사고 이후 서울정형외과에서 보존적 치료를 받았다 [E2].\n\n## 2. 쟁점\n\n사고일자에 대해 진단서와 의무기록 간 3일의 차이가 확인되어 검토가 필요하다 [E3].\n",
        "evidence": {
          "document_path": "outputs/CASE_DEMO/draft_report_v1.md",
          "citations": [
            {
              "tag": "E1",
              "document_id": "DOC_001",
              "page": 1,
              "quote": "요추 압박골절 소견, 2024-03-12 발생"
            },
            {
              "tag": "E2",
              "document_id": "DOC_001",
              "page": 1,
              "quote": "서울정형외과의원 내원, 보존적 치료 시행"
            },
            {
              "tag": "E3",
              "document_id": "DOC_002",
              "page": 3,
              "quote": "내원일 2024-03-15로 기재"
            }
          ],
          "generated_at": "2026-07-10T12:23:54.868807+09:00"
        }
      }
    }
  }
};
export const DEFAULT_CASE_ID = "CASE_001";
