좋아. 지금부터는 각 단계별로 **“컴포넌트 I/O 계약”**을 잡는다고 보면 돼.
즉, 각 agent가 무엇을 받아서 어떤 JSON을 만들어야 하고, 다음 agent는 그중 어떤 필드를 참조해야 하는지를 정하는 단계야.

아래는 우선 **v0.1 초안**으로 잡을 수 있는 형식이야.

---

# 0. 공통 설계 원칙

모든 컴포넌트 output은 아래 공통 필드를 갖도록 하는 것이 좋아.

```json
{
  "case_id": "CASE_001",
  "run_id": "RUN_20260706_001",
  "component": "DocumentClassification",
  "status": "success",
  "created_at": "2026-07-06T15:30:00+09:00",
  "model_info": {
    "model_name": "gpt-4.1",
    "prompt_version": "document_classification_v0.1"
  },
  "confidence": 0.87,
  "review_required": false,
  "reviewer_role": null,
  "evidence_references": [],
  "warnings": []
}
```

판단성 output에는 반드시 아래 안전 필드를 추가한다.

```json
{
  "confidence": 0.78,
  "evidence_references": [
    {
      "document_id": "DOC_003",
      "page": 2,
      "quote": "상해후유장해 담보 가입금액 1억원"
    }
  ],
  "review_required": true,
  "reviewer_role": "손해사정사",
  "source_grounded": true,
  "hallucination_risk_check": {
    "risk_level": "medium",
    "reason": "근거 문서는 있으나 약관 해석 판단 필요"
  },
  "prohibited_language_check": {
    "passed": true,
    "issues": []
  }
}
```

---

# 1. Phase 1 — 최초 청구/검토 파이프라인 I/O

## 1. Case Intake

### 목적

케이스 폴더를 받아서 케이스 ID, 문서 ID, 파일 경로, 원본 문서 목록을 생성한다.

### Input 예시

```json
{
  "case_id": "CASE_001",
  "source_folder": "data/raw/CASE_001/",
  "uploaded_by": "intern_a",
  "received_at": "2026-07-06T10:00:00+09:00",
  "files": [
    {
      "file_name": "diagnosis.pdf",
      "file_path": "data/raw/CASE_001/diagnosis.pdf",
      "mime_type": "application/pdf"
    },
    {
      "file_name": "policy.pdf",
      "file_path": "data/raw/CASE_001/policy.pdf",
      "mime_type": "application/pdf"
    },
    {
      "file_name": "medical_record.pdf",
      "file_path": "data/raw/CASE_001/medical_record.pdf",
      "mime_type": "application/pdf"
    }
  ]
}
```

### Output 예시: `case_manifest.json`

```json
{
  "case_id": "CASE_001",
  "case_name": "pre_existing_condition_case_001",
  "case_status": "intake_completed",
  "source_folder": "data/raw/CASE_001/",
  "created_at": "2026-07-06T10:05:00+09:00",
  "documents": [
    "DOC_001",
    "DOC_002",
    "DOC_003"
  ],
  "privacy_level": "raw_contains_sensitive_info",
  "ground_truth_available": true,
  "reviewers": [
    {
      "role": "손해사정사",
      "name": "reviewer_claims_01"
    },
    {
      "role": "의사",
      "name": "reviewer_medical_01"
    }
  ]
}
```

### Output 예시: `document_manifest.json`

```json
{
  "case_id": "CASE_001",
  "documents": [
    {
      "document_id": "DOC_001",
      "file_name": "diagnosis.pdf",
      "file_path": "data/raw/CASE_001/diagnosis.pdf",
      "mime_type": "application/pdf",
      "document_type": null,
      "pages": null,
      "ocr_status": "pending"
    },
    {
      "document_id": "DOC_002",
      "file_name": "policy.pdf",
      "file_path": "data/raw/CASE_001/policy.pdf",
      "mime_type": "application/pdf",
      "document_type": null,
      "pages": null,
      "ocr_status": "pending"
    }
  ]
}
```

### 다음 단계로 넘기는 핵심 필드

```json
{
  "case_id": "CASE_001",
  "documents": [
    {
      "document_id": "DOC_001",
      "file_path": "data/raw/CASE_001/diagnosis.pdf"
    }
  ]
}
```

---

## 2. OCR / Text Extraction

### 목적

PDF, 이미지 문서를 페이지별 텍스트로 변환하고 OCR 품질 로그를 만든다.

### Input 예시

```json
{
  "case_id": "CASE_001",
  "documents": [
    {
      "document_id": "DOC_001",
      "file_path": "data/raw/CASE_001/diagnosis.pdf",
      "mime_type": "application/pdf"
    }
  ],
  "ocr_options": {
    "engine": "upstage_ocr",
    "save_page_text": true,
    "save_quality_log": true
  }
}
```

### Output 예시: `ocr_result.json`

```json
{
  "case_id": "CASE_001",
  "component": "OCRLayer",
  "status": "success",
  "ocr_engine": "upstage_ocr",
  "documents": [
    {
      "document_id": "DOC_001",
      "file_path": "data/raw/CASE_001/diagnosis.pdf",
      "pages": [
        {
          "page": 1,
          "text_path": "data/processed/CASE_001/DOC_001/page_001.md",
          "text_preview": "진단서\n환자명: 홍길동\n진단명: 요추 추간판탈출증...",
          "mean_confidence": 0.91,
          "low_confidence_blocks": []
        },
        {
          "page": 2,
          "text_path": "data/processed/CASE_001/DOC_001/page_002.md",
          "text_preview": "치료기간: 2024.03.13 ~ 2024.03.20...",
          "mean_confidence": 0.84,
          "low_confidence_blocks": [
            {
              "block_id": "B002",
              "text": "판독 불명확",
              "confidence": 0.42
            }
          ]
        }
      ],
      "document_mean_confidence": 0.875,
      "ocr_status": "completed",
      "review_required": true,
      "review_reason": "page_002 contains low confidence block"
    }
  ]
}
```

### 다음 단계로 넘기는 핵심 필드

```json
{
  "case_id": "CASE_001",
  "document_id": "DOC_001",
  "page_text_paths": [
    "data/processed/CASE_001/DOC_001/page_001.md",
    "data/processed/CASE_001/DOC_001/page_002.md"
  ],
  "document_mean_confidence": 0.875
}
```

---

## 3. Redaction / 가명처리

### 목적

이름, 주민번호, 연락처, 주소, 병원등록번호, 보험증권번호 등 민감정보를 제거하거나 대체한다.

### Input 예시

```json
{
  "case_id": "CASE_001",
  "documents": [
    {
      "document_id": "DOC_001",
      "page_text_paths": [
        "data/processed/CASE_001/DOC_001/page_001.md",
        "data/processed/CASE_001/DOC_001/page_002.md"
      ]
    }
  ],
  "redaction_policy": {
    "replace_person_name": true,
    "replace_rrn": true,
    "replace_phone": true,
    "replace_address": true,
    "replace_policy_number": true
  }
}
```

### Output 예시: `redaction_result.json`

```json
{
  "case_id": "CASE_001",
  "component": "Redaction",
  "status": "success",
  "documents": [
    {
      "document_id": "DOC_001",
      "redacted_text_path": "data/processed/CASE_001/DOC_001/redacted_text.md",
      "redaction_log_path": "data/processed/CASE_001/DOC_001/redaction_log.json",
      "items_redacted": [
        {
          "entity_type": "person_name",
          "original_value_hash": "sha256:abc123",
          "replacement": "[PERSON_001]",
          "page": 1
        },
        {
          "entity_type": "rrn",
          "original_value_hash": "sha256:def456",
          "replacement": "[RRN_001]",
          "page": 1
        }
      ],
      "redaction_confidence": 0.93,
      "review_required": false
    }
  ]
}
```

### 다음 단계로 넘기는 핵심 필드

```json
{
  "case_id": "CASE_001",
  "document_id": "DOC_001",
  "redacted_text_path": "data/processed/CASE_001/DOC_001/redacted_text.md"
}
```

---

## 4. Document Classification

### 목적

문서를 진단서, 의무기록, 보험증권, 약관, 보험사 안내문 등으로 분류한다.

### Input 예시

```json
{
  "case_id": "CASE_001",
  "documents": [
    {
      "document_id": "DOC_001",
      "redacted_text_path": "data/processed/CASE_001/DOC_001/redacted_text.md",
      "text_preview": "진단서\n진단명: 요추 추간판탈출증\nKCD: M51.2..."
    }
  ],
  "candidate_document_types": [
    "diagnosis_certificate",
    "medical_record",
    "insurance_policy",
    "insurance_certificate",
    "receipt",
    "insurer_response",
    "imaging_report",
    "other"
  ]
}
```

### Output 예시: `classification_result.json`

```json
{
  "case_id": "CASE_001",
  "component": "DocumentClassification",
  "status": "success",
  "documents": [
    {
      "document_id": "DOC_001",
      "predicted_document_type": "diagnosis_certificate",
      "document_type_label": "진단서",
      "confidence": 0.94,
      "candidate_types": [
        {
          "document_type": "diagnosis_certificate",
          "confidence": 0.94
        },
        {
          "document_type": "medical_record",
          "confidence": 0.31
        }
      ],
      "evidence_references": [
        {
          "page": 1,
          "quote": "진단서"
        },
        {
          "page": 1,
          "quote": "진단명: 요추 추간판탈출증"
        }
      ],
      "review_required": false
    }
  ]
}
```

### 다음 단계로 넘기는 핵심 필드

```json
{
  "document_id": "DOC_001",
  "document_type": "diagnosis_certificate",
  "confidence": 0.94,
  "redacted_text_path": "data/processed/CASE_001/DOC_001/redacted_text.md"
}
```

---

## 5. Document Preprocessing

### 목적

문서를 검색과 필드 추출에 적합하도록 정리하고 page chunk를 만든다.

### Input 예시

```json
{
  "case_id": "CASE_001",
  "documents": [
    {
      "document_id": "DOC_001",
      "document_type": "diagnosis_certificate",
      "redacted_text_path": "data/processed/CASE_001/DOC_001/redacted_text.md"
    }
  ],
  "chunking_options": {
    "chunk_by": "page_then_section",
    "max_tokens": 800,
    "overlap_tokens": 100
  }
}
```

### Output 예시: `page_chunks.json`

```json
{
  "case_id": "CASE_001",
  "component": "DocumentPreprocessing",
  "status": "success",
  "chunks": [
    {
      "chunk_id": "CHUNK_DOC_001_001",
      "document_id": "DOC_001",
      "document_type": "diagnosis_certificate",
      "page_start": 1,
      "page_end": 1,
      "section_title": "진단 정보",
      "text_path": "data/processed/CASE_001/DOC_001/chunks/chunk_001.md",
      "text_preview": "진단명: 요추 추간판탈출증\nKCD: M51.2\n치료 필요...",
      "tokens": 420
    },
    {
      "chunk_id": "CHUNK_DOC_001_002",
      "document_id": "DOC_001",
      "document_type": "diagnosis_certificate",
      "page_start": 2,
      "page_end": 2,
      "section_title": "치료기간",
      "text_path": "data/processed/CASE_001/DOC_001/chunks/chunk_002.md",
      "text_preview": "치료기간: 2024.03.13부터 2024.03.20까지...",
      "tokens": 260
    }
  ]
}
```

### 다음 단계로 넘기는 핵심 필드

```json
{
  "case_id": "CASE_001",
  "chunks": [
    {
      "chunk_id": "CHUNK_DOC_001_001",
      "document_id": "DOC_001",
      "document_type": "diagnosis_certificate",
      "text_path": "data/processed/CASE_001/DOC_001/chunks/chunk_001.md"
    }
  ]
}
```

---

## 6. Optional Vector Indexing

### 목적

케이스 문서와 약관 문서를 RAG 검색 가능하게 만든다.
PoC 초기에는 보류 가능하고, BM25나 직접 프롬프팅으로 시작해도 된다.

### Input 예시

```json
{
  "case_id": "CASE_001",
  "chunks": [
    {
      "chunk_id": "CHUNK_DOC_001_001",
      "document_id": "DOC_001",
      "document_type": "diagnosis_certificate",
      "text_path": "data/processed/CASE_001/DOC_001/chunks/chunk_001.md"
    }
  ],
  "embedding_model": "text-embedding-3-small",
  "index_name": "case_CASE_001_index"
}
```

### Output 예시: `index_metadata.json`

```json
{
  "case_id": "CASE_001",
  "component": "VectorIndexing",
  "status": "success",
  "index_name": "case_CASE_001_index",
  "index_path": "data/indexes/CASE_001/vector.index",
  "embedding_model": "text-embedding-3-small",
  "indexed_chunks": [
    {
      "chunk_id": "CHUNK_DOC_001_001",
      "document_id": "DOC_001",
      "document_type": "diagnosis_certificate"
    }
  ],
  "total_chunks": 1
}
```

---

## 7. Policy Processing

### 목적

약관 문서를 조항 단위로 나누고, 목차·조항번호·페이지 정보를 구조화한다.

### Input 예시

```json
{
  "case_id": "CASE_001",
  "policy_documents": [
    {
      "document_id": "DOC_002",
      "document_type": "insurance_policy",
      "redacted_text_path": "data/processed/CASE_001/DOC_002/redacted_text.md"
    }
  ]
}
```

### Output 예시: `policy_clause.json`

```json
{
  "case_id": "CASE_001",
  "component": "PolicyProcessing",
  "status": "success",
  "policy_document_id": "DOC_002",
  "clauses": [
    {
      "clause_id": "CLAUSE_001",
      "title": "상해후유장해 보험금",
      "section_path": "보통약관 > 제3조 보험금의 지급사유",
      "article_number": "제3조",
      "page_start": 12,
      "page_end": 13,
      "raw_text": "피보험자가 보험기간 중 상해로 장해상태가 되었을 때...",
      "chunk_id": "CHUNK_DOC_002_012"
    },
    {
      "clause_id": "CLAUSE_002",
      "title": "보험금을 지급하지 않는 사유",
      "section_path": "보통약관 > 제5조 면책사항",
      "article_number": "제5조",
      "page_start": 15,
      "page_end": 16,
      "raw_text": "회사는 다음 중 어느 한 가지로 보험금 지급사유가 발생한 때에는...",
      "chunk_id": "CHUNK_DOC_002_015"
    }
  ],
  "review_required": true,
  "reviewer_role": "손해사정사"
}
```

---

## 8. Policy Clause Extraction

### 목적

약관 조항에서 지급요건, 면책사항, 감액 관련 조건을 추출한다.

### Input 예시

```json
{
  "case_id": "CASE_001",
  "clauses": [
    {
      "clause_id": "CLAUSE_001",
      "title": "상해후유장해 보험금",
      "raw_text": "피보험자가 보험기간 중 상해로 장해상태가 되었을 때..."
    }
  ],
  "target_extraction_types": [
    "coverage_type",
    "payment_condition",
    "exclusion",
    "reduction_condition",
    "required_documents"
  ]
}
```

### Output 예시: `policy_clause_extraction_result.json`

```json
{
  "case_id": "CASE_001",
  "component": "PolicyClauseExtraction",
  "status": "success",
  "extracted_clauses": [
    {
      "clause_id": "CLAUSE_001",
      "coverage_type": "상해후유장해",
      "payment_conditions": [
        "보험기간 중 상해 발생",
        "상해로 장해상태 발생",
        "약관상 장해분류표에 해당"
      ],
      "exclusions": [],
      "required_documents": [
        "진단서",
        "후유장해진단서",
        "의무기록",
        "영상판독지"
      ],
      "confidence": 0.82,
      "evidence_references": [
        {
          "document_id": "DOC_002",
          "page": 12,
          "quote": "상해로 장해상태가 되었을 때"
        }
      ],
      "review_required": true,
      "reviewer_role": "손해사정사"
    }
  ]
}
```

---

## 9. Policy Clause Normalization

### 목적

약관 조항을 표준화된 claim review schema로 변환한다.

### Input 예시

```json
{
  "case_id": "CASE_001",
  "extracted_clauses": [
    {
      "clause_id": "CLAUSE_001",
      "coverage_type": "상해후유장해",
      "payment_conditions": [
        "보험기간 중 상해 발생",
        "상해로 장해상태 발생"
      ]
    }
  ]
}
```

### Output 예시: `normalized_policy_clause.json`

```json
{
  "case_id": "CASE_001",
  "component": "PolicyClauseNormalization",
  "status": "success",
  "normalized_clauses": [
    {
      "clause_id": "CLAUSE_001",
      "normalized_coverage_type": "injury_disability",
      "coverage_label_ko": "상해후유장해",
      "clause_type": "payment_condition",
      "requirements": [
        {
          "requirement_id": "REQ_001",
          "requirement_type": "accident_or_injury",
          "description": "보험기간 중 상해가 발생해야 함"
        },
        {
          "requirement_id": "REQ_002",
          "requirement_type": "disability_status",
          "description": "상해로 인한 장해상태가 확인되어야 함"
        }
      ],
      "exclusions": [],
      "required_documents": [
        "diagnosis_certificate",
        "medical_record",
        "imaging_report",
        "disability_certificate"
      ],
      "confidence": 0.8,
      "review_required": true,
      "reviewer_role": "손해사정사"
    }
  ]
}
```

---

## 10. Claim Field Extraction

### 목적

진단명, KCD, 사고일, 발병일, 수술명, 치료기간, 병원명 등 청구 검토 필드를 추출한다.

### Input 예시

```json
{
  "case_id": "CASE_001",
  "documents": [
    {
      "document_id": "DOC_001",
      "document_type": "diagnosis_certificate",
      "chunks": [
        "CHUNK_DOC_001_001",
        "CHUNK_DOC_001_002"
      ]
    },
    {
      "document_id": "DOC_003",
      "document_type": "medical_record",
      "chunks": [
        "CHUNK_DOC_003_001"
      ]
    }
  ],
  "target_fields": [
    "diagnosis_name",
    "kcd_code",
    "accident_date",
    "onset_date",
    "surgery_name",
    "treatment_period",
    "hospital_name",
    "admission_period"
  ]
}
```

### Output 예시: `extracted_claim_fields.json`

```json
{
  "case_id": "CASE_001",
  "component": "ClaimFieldExtraction",
  "status": "success",
  "fields": {
    "diagnosis_name": {
      "value": "요추 추간판탈출증",
      "normalized_value": "lumbar_disc_herniation",
      "confidence": 0.88,
      "evidence_references": [
        {
          "document_id": "DOC_001",
          "page": 1,
          "quote": "진단명: 요추 추간판탈출증"
        }
      ],
      "review_required": false
    },
    "kcd_code": {
      "value": "M51.2",
      "confidence": 0.82,
      "evidence_references": [
        {
          "document_id": "DOC_001",
          "page": 1,
          "quote": "KCD: M51.2"
        }
      ],
      "review_required": false
    },
    "accident_date": {
      "value": "2024-03-12",
      "confidence": 0.72,
      "evidence_references": [
        {
          "document_id": "DOC_003",
          "page": 2,
          "quote": "2024.03.12 사고 후 요통 발생"
        }
      ],
      "review_required": true,
      "reviewer_role": "손해사정사"
    },
    "admission_period": {
      "start_date": "2024-03-13",
      "end_date": "2024-03-20",
      "days": 8,
      "confidence": 0.86,
      "evidence_references": [
        {
          "document_id": "DOC_001",
          "page": 1,
          "quote": "입원기간: 2024.03.13 ~ 2024.03.20"
        }
      ],
      "review_required": false
    }
  }
}
```

---

## 11. Coverage Identification

### 목적

보험증권, 약관, 청구자료에서 청구 가능한 담보 후보를 식별한다.

### Input 예시

```json
{
  "case_id": "CASE_001",
  "documents": [
    {
      "document_id": "DOC_004",
      "document_type": "insurance_certificate"
    },
    {
      "document_id": "DOC_002",
      "document_type": "insurance_policy"
    }
  ],
  "extracted_claim_fields_path": "outputs/CASE_001/extracted_claim_fields.json",
  "normalized_policy_clause_path": "outputs/CASE_001/normalized_policy_clause.json"
}
```

### Output 예시: `coverage_result.json`

```json
{
  "case_id": "CASE_001",
  "component": "CoverageIdentification",
  "status": "success",
  "claim_coverages": [
    {
      "coverage_id": "COV_001",
      "coverage_type": "injury_disability",
      "coverage_label_ko": "상해후유장해",
      "policy_document_id": "DOC_002",
      "insurance_certificate_document_id": "DOC_004",
      "insured_amount": 100000000,
      "currency": "KRW",
      "confidence": 0.86,
      "evidence_references": [
        {
          "document_id": "DOC_004",
          "page": 3,
          "quote": "상해후유장해 가입금액 1억원"
        }
      ],
      "review_required": true,
      "reviewer_role": "손해사정사"
    },
    {
      "coverage_id": "COV_002",
      "coverage_type": "medical_expense",
      "coverage_label_ko": "실손의료비",
      "insured_amount": null,
      "currency": "KRW",
      "confidence": 0.69,
      "evidence_references": [
        {
          "document_id": "DOC_005",
          "page": 1,
          "quote": "진료비 영수증"
        }
      ],
      "review_required": true,
      "reviewer_role": "손해사정사"
    }
  ]
}
```

---

## 12. Case Type Classification

### 목적

사건 유형을 분류하여 손사서 템플릿과 검토 경로를 선택한다.

### Input 예시

```json
{
  "case_id": "CASE_001",
  "extracted_claim_fields_path": "outputs/CASE_001/extracted_claim_fields.json",
  "coverage_result_path": "outputs/CASE_001/coverage_result.json",
  "document_manifest_path": "outputs/CASE_001/document_manifest.json",
  "candidate_case_types": [
    "후유장해",
    "진단비",
    "수술비",
    "실손",
    "배상책임",
    "기타"
  ]
}
```

### Output 예시: `case_type_result.json`

```json
{
  "case_id": "CASE_001",
  "component": "CaseTypeClassification",
  "status": "success",
  "case_type": {
    "primary_type": "후유장해",
    "secondary_types": [
      "기왕증 감액"
    ],
    "template_id": "draft_template_disability_v0.1",
    "confidence": 0.84,
    "evidence_references": [
      {
        "document_id": "DOC_004",
        "page": 3,
        "quote": "상해후유장해 가입금액"
      },
      {
        "document_id": "DOC_002",
        "page": 12,
        "quote": "장해상태가 되었을 때"
      }
    ],
    "review_required": true,
    "reviewer_role": "손해사정사"
  }
}
```

---

## 13. Requirement Matching

### 목적

약관상 지급요건과 청구 자료의 추출값을 매칭한다.

### Input 예시

```json
{
  "case_id": "CASE_001",
  "coverage_result_path": "outputs/CASE_001/coverage_result.json",
  "normalized_policy_clause_path": "outputs/CASE_001/normalized_policy_clause.json",
  "extracted_claim_fields_path": "outputs/CASE_001/extracted_claim_fields.json"
}
```

### Output 예시: `requirement_matching_result.json`

```json
{
  "case_id": "CASE_001",
  "component": "RequirementMatching",
  "status": "success",
  "matches": [
    {
      "coverage_id": "COV_001",
      "clause_id": "CLAUSE_001",
      "requirement_id": "REQ_001",
      "requirement_description": "보험기간 중 상해가 발생해야 함",
      "match_status": "supported",
      "matched_fields": [
        "accident_date"
      ],
      "confidence": 0.76,
      "evidence_references": [
        {
          "document_id": "DOC_003",
          "page": 2,
          "quote": "2024.03.12 사고 후 요통 발생"
        }
      ],
      "review_required": true,
      "reviewer_role": "손해사정사"
    },
    {
      "coverage_id": "COV_001",
      "clause_id": "CLAUSE_001",
      "requirement_id": "REQ_002",
      "requirement_description": "상해로 인한 장해상태가 확인되어야 함",
      "match_status": "insufficient_evidence",
      "matched_fields": [],
      "confidence": 0.58,
      "evidence_references": [],
      "review_required": true,
      "reviewer_role": "의사"
    }
  ]
}
```

---

## 14. Evidence Validation

### 목적

추출값과 문서 근거를 검증하고, 문서 간 불일치를 명시적으로 출력한다.

### Input 예시

```json
{
  "case_id": "CASE_001",
  "extracted_claim_fields_path": "outputs/CASE_001/extracted_claim_fields.json",
  "requirement_matching_result_path": "outputs/CASE_001/requirement_matching_result.json",
  "document_chunks_path": "outputs/CASE_001/page_chunks.json"
}
```

### Output 예시: `evidence_validation_result.json`

```json
{
  "case_id": "CASE_001",
  "component": "EvidenceValidation",
  "status": "success",
  "validated_claims": [
    {
      "claim_id": "VC_001",
      "claim": "진단명은 요추 추간판탈출증으로 확인됨",
      "validation_status": "supported",
      "confidence": 0.88,
      "evidence_references": [
        {
          "document_id": "DOC_001",
          "page": 1,
          "quote": "진단명: 요추 추간판탈출증"
        }
      ],
      "review_required": false
    }
  ],
  "inconsistencies": [
    {
      "inconsistency_id": "INC_001",
      "field": "accident_date",
      "description": "사고일이 문서별로 다르게 기재됨",
      "values": [
        {
          "document_id": "DOC_003",
          "document_type": "medical_record",
          "value": "2024-03-12",
          "page": 2
        },
        {
          "document_id": "DOC_006",
          "document_type": "insurer_response",
          "value": "2024-03-15",
          "page": 1
        }
      ],
      "severity": "medium",
      "confidence": 0.81,
      "review_required": true,
      "reviewer_role": "손해사정사"
    }
  ],
  "hallucination_risk_check": {
    "risk_level": "low",
    "reason": "대부분의 claim이 직접 근거 문장과 연결됨"
  }
}
```

---

## 15. Screening Report Generation

### 목적

초기 검토용 스크리닝 리포트를 생성한다.

### Input 예시

```json
{
  "case_id": "CASE_001",
  "case_manifest_path": "outputs/CASE_001/case_manifest.json",
  "classification_result_path": "outputs/CASE_001/classification_result.json",
  "extracted_claim_fields_path": "outputs/CASE_001/extracted_claim_fields.json",
  "coverage_result_path": "outputs/CASE_001/coverage_result.json",
  "case_type_result_path": "outputs/CASE_001/case_type_result.json",
  "requirement_matching_result_path": "outputs/CASE_001/requirement_matching_result.json",
  "evidence_validation_result_path": "outputs/CASE_001/evidence_validation_result.json",
  "denial_reason_result_path": "outputs/CASE_001/denial_reason_result.json"
}
```

### Output 예시: `screening_report.json`

```json
{
  "case_id": "CASE_001",
  "component": "ScreeningReportGeneration",
  "status": "success",
  "report_path": "outputs/CASE_001/screening_report.md",
  "summary": {
    "case_type": "후유장해",
    "main_diagnosis": "요추 추간판탈출증",
    "claim_coverages": [
      "상해후유장해"
    ],
    "main_issue": "기왕증 기여도 적용의 적정성"
  },
  "sections": [
    {
      "section_id": "screening_01",
      "title": "사건 개요",
      "source_json": [
        "extracted_claim_fields.json",
        "case_type_result.json"
      ]
    },
    {
      "section_id": "screening_02",
      "title": "보험사 판단",
      "source_json": [
        "denial_reason_result.json"
      ]
    },
    {
      "section_id": "screening_03",
      "title": "핵심 쟁점",
      "source_json": [
        "requirement_matching_result.json",
        "evidence_validation_result.json"
      ]
    }
  ],
  "review_points": [
    {
      "point": "기왕증 기여도 50% 적용의 타당성 검토 필요",
      "reviewer_role": "의사",
      "priority": "high"
    },
    {
      "point": "상해후유장해 담보 지급요건 충족 여부 검토 필요",
      "reviewer_role": "손해사정사",
      "priority": "high"
    }
  ],
  "confidence": 0.76,
  "review_required": true,
  "reviewer_role": "손해사정사"
}
```

### Markdown Output 예시: `screening_report.md`

```markdown
# 스크리닝 리포트

## 1. 사건 개요
- 사건 ID: CASE_001
- 사건 유형: 후유장해
- 주요 진단명: 요추 추간판탈출증
- 사고일: 2024-03-12
- 주요 청구담보: 상해후유장해

## 2. 보험사 판단
- 감액/부지급 여부: 감액
- 감액사유: 기왕증 기여도
- 보험사 주장 요약: 기왕증 기여도 50%를 적용한 것으로 보임

## 3. 핵심 쟁점
- 기왕증 기여도 적용 비율의 적정성
- 사고 이후 증상 악화 여부
- 장해상태와 사고 사이의 관련성

## 4. 문서 간 불일치
- 사고일이 의무기록과 보험사 안내문에서 다르게 기재됨

## 5. 전문가 검수 포인트
- 의사 검수: 기왕증 기여도 및 사고 기여도 판단
- 손해사정사 검수: 약관상 지급요건 충족 여부
```

---

## 16. Draft Report Generation v1

### 목적

스크리닝 리포트와 근거자료를 바탕으로 손해사정서 초안 v1을 작성한다.

### Input 예시

```json
{
  "case_id": "CASE_001",
  "screening_report_path": "outputs/CASE_001/screening_report.md",
  "normalized_policy_clause_path": "outputs/CASE_001/normalized_policy_clause.json",
  "requirement_matching_result_path": "outputs/CASE_001/requirement_matching_result.json",
  "evidence_validation_result_path": "outputs/CASE_001/evidence_validation_result.json",
  "case_type_result_path": "outputs/CASE_001/case_type_result.json",
  "template_id": "draft_template_disability_v0.1"
}
```

### Output 예시: `draft_report_metadata.json`

```json
{
  "case_id": "CASE_001",
  "component": "DraftReportGeneration",
  "status": "success",
  "draft_id": "DRAFT_001",
  "version": "v1",
  "draft_path": "outputs/CASE_001/draft_report_v1.md",
  "template_id": "draft_template_disability_v0.1",
  "source_files": [
    "screening_report.json",
    "normalized_policy_clause.json",
    "requirement_matching_result.json",
    "evidence_validation_result.json"
  ],
  "sections": [
    {
      "section_title": "사건 개요",
      "review_required": false
    },
    {
      "section_title": "약관 검토",
      "review_required": true,
      "reviewer_role": "손해사정사"
    },
    {
      "section_title": "의학적 검토",
      "review_required": true,
      "reviewer_role": "의사"
    }
  ],
  "confidence": 0.72,
  "review_required": true,
  "reviewer_role": "손해사정사"
}
```

### Markdown Output 예시: `draft_report_v1.md`

```markdown
# 손해사정서 초안 v1

## 1. 사건 개요
본 건은 피보험자가 2024년 3월 12일 사고 이후 요추 부위 통증을 호소하고, 진단서상 요추 추간판탈출증 진단을 받은 사안이다.

## 2. 관련 자료
- 진단서: DOC_001
- 의무기록: DOC_003
- 보험증권: DOC_004
- 약관: DOC_002

## 3. 주요 쟁점
본 건의 주요 쟁점은 상해후유장해 담보의 지급요건 충족 여부와 기왕증 기여도 적용의 적정성이다.

## 4. 약관 검토
약관상 상해후유장해 보험금은 보험기간 중 상해로 장해상태가 발생한 경우 검토 대상이 된다.  
다만, 해당 조항의 구체적 적용 여부는 손해사정사의 검수가 필요하다.

## 5. 의학적 검토
의무기록상 사고 이후 통증 증가와 치료 지속성이 일부 확인된다.  
다만, 기왕증 기여도와 사고 기여도 판단은 전문의 검수가 필요하다.

## 6. 손해사정 의견
현재 자료 기준으로는 보험사의 기왕증 기여도 적용 비율에 대해 추가 검토 여지가 있다.  
본 초안은 검수용이며 최종 판단은 전문가 검토 후 확정되어야 한다.
```

---

## 17. Critic Agent

### 목적

초안에서 근거 없는 주장, 법률·의료 단정, 금지 표현, 환각 위험 문장을 탐지한다.

### Input 예시

```json
{
  "case_id": "CASE_001",
  "draft_id": "DRAFT_001",
  "draft_path": "outputs/CASE_001/draft_report_v1.md",
  "evidence_validation_result_path": "outputs/CASE_001/evidence_validation_result.json",
  "prohibited_language_rules_path": "prompts/common/prohibited_language_rules.md"
}
```

### Output 예시: `critic_result.json`

```json
{
  "case_id": "CASE_001",
  "component": "CriticAgent",
  "status": "success",
  "draft_id": "DRAFT_001",
  "critic_items": [
    {
      "sentence_id": "SENT_014",
      "sentence": "보험사는 보험금을 지급해야 한다.",
      "issue_type": "prohibited_legal_conclusion",
      "severity": "high",
      "reason": "지급 여부를 단정하는 표현",
      "suggested_revision": "보험금 지급 가능성을 검토할 여지가 있다.",
      "review_required": true,
      "reviewer_role": "손해사정사"
    },
    {
      "sentence_id": "SENT_021",
      "sentence": "의학적으로 명백히 사고와 관련된다.",
      "issue_type": "unsupported_medical_causation",
      "severity": "high",
      "reason": "의학적 인과관계를 단정함",
      "suggested_revision": "의무기록상 사고 이후 증상 변화가 확인되며, 사고와의 관련성은 전문의 검수가 필요하다.",
      "review_required": true,
      "reviewer_role": "의사"
    }
  ],
  "overall_result": {
    "prohibited_language_check_passed": false,
    "hallucination_risk_level": "medium",
    "source_grounding_score": 0.74,
    "review_required": true
  },
  "reviewed_draft_path": "outputs/CASE_001/draft_report_v1_reviewed.md"
}
```

---

## 18. Human Review → Evaluation

### Human Review Input 예시

```json
{
  "case_id": "CASE_001",
  "draft_path": "outputs/CASE_001/draft_report_v1_reviewed.md",
  "critic_result_path": "outputs/CASE_001/critic_result.json",
  "screening_report_path": "outputs/CASE_001/screening_report.md",
  "reviewer_role": "손해사정사"
}
```

### Human Review Output 예시: `expert_review.json`

```json
{
  "case_id": "CASE_001",
  "reviewer_role": "손해사정사",
  "review_status": "completed",
  "scores": {
    "field_extraction_quality": 4,
    "coverage_identification_quality": 4,
    "policy_mapping_usefulness": 3,
    "screening_report_usefulness": 4,
    "draft_report_usability": 3
  },
  "comments": [
    {
      "target": "약관 검토",
      "comment": "약관 조항 후보는 출발점으로 유용하나, 실제 적용 여부는 추가 검토 필요"
    },
    {
      "target": "의학적 검토",
      "comment": "기왕증 기여도 관련 문장은 의사 검수가 필요함"
    }
  ],
  "go_no_go_opinion": "go_with_improvements"
}
```

### Evaluation Output 예시: `evaluation_result.json`

```json
{
  "case_id": "CASE_001",
  "component": "EvaluationHarness",
  "status": "success",
  "run_id": "RUN_20260706_001",
  "metrics": {
    "field_extraction_accuracy": 0.82,
    "coverage_identification_accuracy": 0.78,
    "case_type_accuracy": 1.0,
    "denial_reason_top3_accuracy": 0.8,
    "policy_clause_top3_hit_rate": 0.67,
    "draft_generation_success": true,
    "processing_time_minutes": 8.4
  },
  "human_review_scores": {
    "screening_report_usefulness": 4,
    "rebuttal_point_usefulness": 3,
    "draft_report_usability": 3,
    "medical_expression_safety": 4
  },
  "failure_modes": [
    {
      "category": "ocr",
      "description": "일부 의무기록 페이지에서 OCR confidence 낮음"
    },
    {
      "category": "policy_mapping",
      "description": "약관 조항 후보가 넓게 잡힘"
    }
  ],
  "go_no_go": "go_with_improvements"
}
```

---

# 2. Phase 2 — 보험사 반려/감액 이후 대응 파이프라인 I/O

## 1. Insurer Response Intake

### 목적

보험사 안내문, 감액 통지서, 부지급 안내문을 Phase 2 입력으로 등록한다.

### Input 예시

```json
{
  "case_id": "CASE_001",
  "phase": "phase2_denial_response",
  "new_documents": [
    {
      "file_name": "insurer_reduction_notice.pdf",
      "file_path": "data/raw/CASE_001/insurer_reduction_notice.pdf",
      "mime_type": "application/pdf"
    }
  ],
  "existing_phase1_outputs": {
    "case_manifest_path": "outputs/CASE_001/case_manifest.json",
    "draft_report_v1_path": "outputs/CASE_001/draft_report_v1.md",
    "normalized_policy_clause_path": "outputs/CASE_001/normalized_policy_clause.json",
    "evidence_validation_result_path": "outputs/CASE_001/evidence_validation_result.json"
  }
}
```

### Output 예시: `insurer_response_result.json`

```json
{
  "case_id": "CASE_001",
  "component": "InsurerResponseIntake",
  "status": "success",
  "insurer_response_documents": [
    {
      "document_id": "DOC_020",
      "file_name": "insurer_reduction_notice.pdf",
      "file_path": "data/raw/CASE_001/insurer_reduction_notice.pdf",
      "document_type": "insurer_response",
      "response_type": "reduction_notice",
      "received_date": "2026-07-06"
    }
  ],
  "linked_phase1_outputs": {
    "draft_report_v1_path": "outputs/CASE_001/draft_report_v1.md",
    "normalized_policy_clause_path": "outputs/CASE_001/normalized_policy_clause.json"
  }
}
```

---

## 2~3. OCR → Redaction

Phase 2의 OCR과 Redaction은 Phase 1과 동일한 schema를 재사용한다.
단, output 파일명은 insurer response 기준으로 분리한다.

```json
{
  "case_id": "CASE_001",
  "document_id": "DOC_020",
  "ocr_text_path": "data/processed/CASE_001/DOC_020/insurer_response_text.md",
  "redacted_text_path": "data/processed/CASE_001/DOC_020/redacted_insurer_response.md",
  "document_mean_confidence": 0.89,
  "review_required": false
}
```

---

## 4. Denial / Reduction Reason Extraction

### 목적

보험사 문서에서 반려·감액·부지급 사유 원문을 추출한다.

### Input 예시

```json
{
  "case_id": "CASE_001",
  "insurer_response_documents": [
    {
      "document_id": "DOC_020",
      "redacted_text_path": "data/processed/CASE_001/DOC_020/redacted_insurer_response.md"
    }
  ],
  "target_items": [
    "denial_reason",
    "reduction_reason",
    "excluded_amount",
    "insurer_claim",
    "requested_documents"
  ]
}
```

### Output 예시: `denial_reason_extraction_result.json`

```json
{
  "case_id": "CASE_001",
  "component": "DenialReasonExtraction",
  "status": "success",
  "reason_candidates": [
    {
      "reason_id": "DR_001",
      "reason_type": "reduction",
      "raw_reason_text": "기왕증 기여도 50%를 적용하여 보험금을 감액합니다.",
      "insurer_claim_summary": "보험사는 기존 질환의 기여도를 이유로 보험금을 50% 감액한 것으로 보임",
      "reduction_rate": 0.5,
      "reduction_amount": null,
      "requested_documents": [],
      "confidence": 0.9,
      "evidence_references": [
        {
          "document_id": "DOC_020",
          "page": 1,
          "quote": "기왕증 기여도 50%를 적용"
        }
      ],
      "review_required": true,
      "reviewer_role": "손해사정사"
    }
  ]
}
```

---

## 5. Denial Reason Taxonomy Classification

### 목적

추출된 사유를 표준 R코드 taxonomy로 분류한다.

### Input 예시

```json
{
  "case_id": "CASE_001",
  "reason_candidates": [
    {
      "reason_id": "DR_001",
      "raw_reason_text": "기왕증 기여도 50%를 적용하여 보험금을 감액합니다."
    }
  ],
  "taxonomy_path": "taxonomy/reduction-reasons.md"
}
```

### Output 예시: `denial_reason_result.json`

```json
{
  "case_id": "CASE_001",
  "component": "DenialReasonTaxonomyClassification",
  "status": "success",
  "denial_reasons": [
    {
      "reason_id": "DR_001",
      "taxonomy_code": "R01",
      "taxonomy_label": "기왕증 / 기존 질환 기여도",
      "reason_type": "reduction",
      "raw_reason_text": "기왕증 기여도 50%를 적용하여 보험금을 감액합니다.",
      "insurer_claim_summary": "기존 질환의 기여도를 이유로 보험금 일부 감액",
      "reduction_rate": 0.5,
      "confidence": 0.88,
      "evidence_references": [
        {
          "document_id": "DOC_020",
          "page": 1,
          "quote": "기왕증 기여도 50%"
        }
      ],
      "review_required": true,
      "reviewer_role": "손해사정사"
    }
  ]
}
```

---

## 6. Policy-to-Denial Matching

### 목적

반려·감액 사유와 관련 약관 조항을 연결한다.

### Input 예시

```json
{
  "case_id": "CASE_001",
  "denial_reason_result_path": "outputs/CASE_001/denial_reason_result.json",
  "normalized_policy_clause_path": "outputs/CASE_001/normalized_policy_clause.json",
  "coverage_result_path": "outputs/CASE_001/coverage_result.json"
}
```

### Output 예시: `policy_to_denial_matching_result.json`

```json
{
  "case_id": "CASE_001",
  "component": "PolicyToDenialMatching",
  "status": "success",
  "matches": [
    {
      "reason_id": "DR_001",
      "taxonomy_code": "R01",
      "matched_clauses": [
        {
          "clause_id": "CLAUSE_001",
          "clause_title": "상해후유장해 보험금",
          "match_reason": "상해와 장해상태의 관련성 및 지급요건 검토에 필요",
          "match_score": 0.78,
          "evidence_references": [
            {
              "document_id": "DOC_002",
              "page": 12,
              "quote": "상해로 장해상태가 되었을 때"
            }
          ]
        }
      ],
      "review_required": true,
      "reviewer_role": "손해사정사"
    }
  ]
}
```

---

## 7. Evidence Retrieval

### 목적

기존 Phase 1 자료에서 보험사 주장과 관련된 근거 문서를 검색한다.

### Input 예시

```json
{
  "case_id": "CASE_001",
  "denial_reason_result_path": "outputs/CASE_001/denial_reason_result.json",
  "query": "기왕증 기여도 사고 이후 증상 악화 치료 지속성",
  "search_targets": [
    "medical_record",
    "diagnosis_certificate",
    "imaging_report",
    "policy"
  ],
  "top_k": 5
}
```

### Output 예시: `retrieved_evidence.json`

```json
{
  "case_id": "CASE_001",
  "component": "EvidenceRetrieval",
  "status": "success",
  "retrieved_items": [
    {
      "retrieval_id": "RET_001",
      "reason_id": "DR_001",
      "document_id": "DOC_003",
      "document_type": "medical_record",
      "page": 5,
      "chunk_id": "CHUNK_DOC_003_005",
      "text": "사고 이후 요통 증가 및 하지 방사통 호소...",
      "retrieval_score": 0.82,
      "why_relevant": "사고 이후 증상 악화 여부와 관련"
    },
    {
      "retrieval_id": "RET_002",
      "reason_id": "DR_001",
      "document_id": "DOC_001",
      "document_type": "diagnosis_certificate",
      "page": 1,
      "chunk_id": "CHUNK_DOC_001_001",
      "text": "진단명: 요추 추간판탈출증",
      "retrieval_score": 0.77,
      "why_relevant": "진단명 확인 근거"
    }
  ]
}
```

---

## 8. Validation Against Denial

### 목적

보험사 주장과 기존 자료가 서로 부합하는지, 반박 여지가 있는지 검토한다.

### Input 예시

```json
{
  "case_id": "CASE_001",
  "denial_reason_result_path": "outputs/CASE_001/denial_reason_result.json",
  "retrieved_evidence_path": "outputs/CASE_001/retrieved_evidence.json",
  "policy_to_denial_matching_result_path": "outputs/CASE_001/policy_to_denial_matching_result.json"
}
```

### Output 예시: `denial_validation_result.json`

```json
{
  "case_id": "CASE_001",
  "component": "ValidationAgainstDenial",
  "status": "success",
  "validation_items": [
    {
      "reason_id": "DR_001",
      "insurer_claim": "기왕증 기여도 50%를 적용",
      "validation_status": "needs_expert_review",
      "ai_assessment": "의무기록상 사고 이후 증상 악화 정황이 있어 기왕증 기여도 50% 적용의 적정성은 추가 검토가 필요함",
      "supporting_evidence_for_insurer": [],
      "potential_counter_evidence": [
        {
          "document_id": "DOC_003",
          "page": 5,
          "quote": "사고 이후 요통 증가"
        }
      ],
      "confidence": 0.7,
      "review_required": true,
      "reviewer_role": "의사",
      "source_grounded": true,
      "hallucination_risk_check": {
        "risk_level": "medium",
        "reason": "의학적 인과관계 판단 필요"
      }
    }
  ]
}
```

---

## 9. Rebuttal Point Generation

### 목적

반려·감액 사유별 반박 포인트 후보를 생성한다.

### Input 예시

```json
{
  "case_id": "CASE_001",
  "denial_reason_result_path": "outputs/CASE_001/denial_reason_result.json",
  "denial_validation_result_path": "outputs/CASE_001/denial_validation_result.json",
  "policy_to_denial_matching_result_path": "outputs/CASE_001/policy_to_denial_matching_result.json"
}
```

### Output 예시: `rebuttal_points.json`

```json
{
  "case_id": "CASE_001",
  "component": "RebuttalPointGeneration",
  "status": "success",
  "rebuttal_points": [
    {
      "rebuttal_id": "RB_001",
      "reason_id": "DR_001",
      "taxonomy_code": "R01",
      "insurer_claim": "기왕증 기여도 50% 적용",
      "rebuttal_point": "의무기록상 사고 이후 증상 악화 및 치료 지속성이 확인되므로, 기왕증 기여도 50% 적용의 적정성에 대한 추가 검토가 필요함",
      "supporting_evidence": [
        {
          "document_id": "DOC_003",
          "page": 5,
          "quote": "사고 이후 요통 증가"
        }
      ],
      "related_policy_clauses": [
        {
          "clause_id": "CLAUSE_001",
          "reason": "상해와 장해상태 관련 지급요건 검토"
        }
      ],
      "confidence": 0.72,
      "review_required": true,
      "reviewer_role": "의사",
      "source_grounded": true,
      "prohibited_language_check": {
        "passed": true,
        "issues": []
      },
      "hallucination_risk_check": {
        "risk_level": "medium",
        "reason": "의학적 기여도 판단은 전문의 검수가 필요"
      }
    }
  ],
  "report_path": "outputs/CASE_001/rebuttal_points.md"
}
```

### Markdown Output 예시: `rebuttal_points.md`

```markdown
# 반박 포인트

## 1. 감액사유
- R01: 기왕증 / 기존 질환 기여도
- 보험사 주장: 기왕증 기여도 50% 적용

## 2. 반박 후보
의무기록상 사고 이후 증상 악화 및 치료 지속성이 확인되므로, 기왕증 기여도 50% 적용의 적정성에 대한 추가 검토가 필요하다.

## 3. 근거 자료
- DOC_003 p.5: 사고 이후 요통 증가
- DOC_001 p.1: 요추 추간판탈출증 진단

## 4. 검수 필요
- 기왕증 기여도와 사고 기여도 판단은 전문의 검수가 필요하다.
```

---

## 10. Draft Report Update

### 목적

반박 포인트를 반영해 손사서 초안 v1을 v2로 업데이트한다.

### Input 예시

```json
{
  "case_id": "CASE_001",
  "draft_report_v1_path": "outputs/CASE_001/draft_report_v1.md",
  "rebuttal_points_path": "outputs/CASE_001/rebuttal_points.json",
  "denial_validation_result_path": "outputs/CASE_001/denial_validation_result.json",
  "update_policy": {
    "preserve_existing_sections": true,
    "add_rebuttal_section": true,
    "mark_review_required_sentences": true
  }
}
```

### Output 예시: `draft_report_update_result.json`

```json
{
  "case_id": "CASE_001",
  "component": "DraftReportUpdate",
  "status": "success",
  "previous_draft_id": "DRAFT_001",
  "new_draft_id": "DRAFT_002",
  "previous_version": "v1",
  "new_version": "v2",
  "draft_report_v2_path": "outputs/CASE_001/draft_report_v2.md",
  "updated_sections": [
    {
      "section_title": "감액/부지급 사유에 대한 검토",
      "update_type": "added",
      "source": "rebuttal_points.json",
      "review_required": true,
      "reviewer_role": "손해사정사"
    },
    {
      "section_title": "의학적 검토",
      "update_type": "modified",
      "source": "denial_validation_result.json",
      "review_required": true,
      "reviewer_role": "의사"
    }
  ],
  "confidence": 0.73,
  "review_required": true,
  "reviewer_role": "손해사정사"
}
```

### Markdown Output 예시: `draft_report_v2.md`

```markdown
# 손해사정서 초안 v2

## 1. 사건 개요
본 건은 피보험자가 2024년 3월 12일 사고 이후 요추 부위 통증을 호소하고, 진단서상 요추 추간판탈출증 진단을 받은 사안이다.

## 2. 주요 쟁점
본 건의 주요 쟁점은 상해후유장해 담보의 지급요건 충족 여부와 보험사가 적용한 기왕증 기여도 50%의 적정성이다.

## 3. 감액사유에 대한 검토
보험사는 기왕증 기여도 50%를 적용한 것으로 보인다.  
그러나 의무기록상 사고 이후 증상 악화 및 치료 지속성이 확인되므로, 해당 기여도 적용 비율의 적정성은 추가 검토가 필요하다.

## 4. 근거 자료
- DOC_003 p.5: 사고 이후 요통 증가
- DOC_001 p.1: 요추 추간판탈출증 진단
- DOC_002 p.12: 상해후유장해 관련 지급요건

## 5. 검수 필요 사항
- 의사 검수: 기왕증 기여도와 사고 기여도 판단
- 손해사정사 검수: 약관상 지급요건 충족 여부
```

---

# 3. 전체 컴포넌트 I/O 파일 매핑 요약

| Phase   | 컴포넌트                      | Input 파일                      | Output 파일                                               |
| ------- | ------------------------- | ----------------------------- | ------------------------------------------------------- |
| Phase 1 | Case Intake               | raw case folder               | `case_manifest.json`, `document_manifest.json`          |
| Phase 1 | OCR                       | raw documents                 | `ocr_result.json`, `page_*.md`                          |
| Phase 1 | Redaction                 | `page_*.md`                   | `redacted_text.md`, `redaction_result.json`             |
| Phase 1 | Classification            | `redacted_text.md`            | `classification_result.json`                            |
| Phase 1 | Preprocessing             | classified documents          | `page_chunks.json`                                      |
| Phase 1 | Vector Indexing           | `page_chunks.json`            | `index_metadata.json`                                   |
| Phase 1 | Policy Processing         | policy document chunks        | `policy_clause.json`                                    |
| Phase 1 | Clause Extraction         | `policy_clause.json`          | `policy_clause_extraction_result.json`                  |
| Phase 1 | Clause Normalization      | extracted clauses             | `normalized_policy_clause.json`                         |
| Phase 1 | Field Extraction          | diagnosis, medical records    | `extracted_claim_fields.json`                           |
| Phase 1 | Coverage Identification   | policy, certificate           | `coverage_result.json`                                  |
| Phase 1 | Case Type Classification  | fields, coverage              | `case_type_result.json`                                 |
| Phase 1 | Requirement Matching      | coverage, clauses, fields     | `requirement_matching_result.json`                      |
| Phase 1 | Evidence Validation       | fields, matching, chunks      | `evidence_validation_result.json`                       |
| Phase 1 | Screening Report          | all structured outputs        | `screening_report.json`, `screening_report.md`          |
| Phase 1 | Draft Generation          | screening, evidence, template | `draft_report_metadata.json`, `draft_report_v1.md`      |
| Phase 1 | Critic                    | draft v1                      | `critic_result.json`, `draft_report_v1_reviewed.md`     |
| Phase 1 | Evaluation                | outputs, ground truth         | `evaluation_result.json`                                |
| Phase 2 | Insurer Response Intake   | insurer document              | `insurer_response_result.json`                          |
| Phase 2 | Denial Extraction         | insurer response text         | `denial_reason_extraction_result.json`                  |
| Phase 2 | Taxonomy Classification   | reason candidates             | `denial_reason_result.json`                             |
| Phase 2 | Policy-to-Denial Matching | denial reasons, clauses       | `policy_to_denial_matching_result.json`                 |
| Phase 2 | Evidence Retrieval        | denial reasons, case index    | `retrieved_evidence.json`                               |
| Phase 2 | Validation Against Denial | denial, evidence, policy      | `denial_validation_result.json`                         |
| Phase 2 | Rebuttal Generation       | denial validation             | `rebuttal_points.json`, `rebuttal_points.md`            |
| Phase 2 | Draft Update              | draft v1, rebuttal            | `draft_report_update_result.json`, `draft_report_v2.md` |
| Phase 2 | Critic                    | draft v2                      | `critic_result_v2.json`, `draft_report_v2_reviewed.md`  |
| Phase 2 | Evaluation                | v2 outputs, review            | `evaluation_result.json`                                |

---

# 4. 추천 작업 순서

지금 당장 모든 schema를 완성하려고 하기보다, 아래 순서로 만들면 좋아.

## 1단계: 공통 output 계약 확정

먼저 이 파일부터 만든다.

```text
templates/component-output.md
schemas/common_component_output.schema.json
```

## 2단계: Week 1 schema 확정

```text
schemas/case_manifest.schema.json
schemas/document_manifest.schema.json
schemas/ocr_result.schema.json
schemas/redaction_result.schema.json
schemas/classification_result.schema.json
schemas/extracted_claim_fields.schema.json
```

## 3단계: Week 2 schema 확정

```text
schemas/policy_clause.schema.json
schemas/normalized_policy_clause.schema.json
schemas/coverage_result.schema.json
schemas/case_type_result.schema.json
schemas/requirement_matching_result.schema.json
schemas/evidence_validation_result.schema.json
schemas/denial_reason_result.schema.json
schemas/screening_report.schema.json
```

## 4단계: Week 3 schema 확정

```text
schemas/rebuttal_points.schema.json
schemas/draft_report_metadata.schema.json
schemas/draft_report_update_result.schema.json
schemas/critic_result.schema.json
schemas/expert_review.schema.json
schemas/evaluation_result.schema.json
```

---

# 5. 가장 먼저 확정해야 할 핵심 I/O 5개

PoC 기준으로는 아래 5개만 먼저 안정화해도 파이프라인의 뼈대가 잡혀.

```text
1. document_manifest.json
2. classification_result.json
3. extracted_claim_fields.json
4. denial_reason_result.json
5. screening_report.json
```

이 5개가 확정되면 나머지는 자연스럽게 붙일 수 있다.


이 5개는 **각 agent가 다음 단계로 넘겨주는 핵심 “중간 산출물 JSON”**이야.
쉽게 말하면, AI가 문서를 읽고 판단하기 전에 **업무 기록지처럼 남기는 구조화 파일**이라고 보면 돼.

---

# 1. `document_manifest.json`

## 한 줄 정의

**이 케이스에 어떤 문서들이 들어왔는지 정리한 문서 목록표.**

## 왜 필요한가?

처음 케이스 폴더를 받으면 PDF나 이미지가 여러 개 있을 거야.

예를 들면:

```text
diagnosis.pdf
medical_record.pdf
policy.pdf
insurance_certificate.pdf
receipt.pdf
insurer_notice.pdf
```

그런데 agent는 파일명만 보고는 각 문서가 뭔지, OCR이 끝났는지, 어디에 저장됐는지 알 수 없어.
그래서 `document_manifest.json`이 “케이스 안의 모든 문서 목록과 상태”를 관리한다.

## 담기는 내용

* 문서 ID
* 원본 파일 경로
* 문서 유형
* OCR 상태
* OCR 텍스트 경로
* 가명처리 텍스트 경로
* 페이지 수
* 분류 confidence

## 예시

```json
{
  "case_id": "CASE_001",
  "documents": [
    {
      "document_id": "DOC_001",
      "file_name": "diagnosis.pdf",
      "file_path": "data/raw/CASE_001/diagnosis.pdf",
      "document_type": "diagnosis_certificate",
      "pages": 2,
      "ocr_status": "completed",
      "ocr_text_path": "data/processed/CASE_001/DOC_001/text.md",
      "redacted_text_path": "data/processed/CASE_001/DOC_001/redacted_text.md",
      "classification_confidence": 0.94
    },
    {
      "document_id": "DOC_002",
      "file_name": "policy.pdf",
      "file_path": "data/raw/CASE_001/policy.pdf",
      "document_type": "insurance_policy",
      "pages": 35,
      "ocr_status": "completed",
      "ocr_text_path": "data/processed/CASE_001/DOC_002/text.md",
      "redacted_text_path": "data/processed/CASE_001/DOC_002/redacted_text.md",
      "classification_confidence": 0.91
    }
  ]
}
```

## 누가 만든다?

`CaseIntakeAgent`가 처음 만들고, 이후 OCR과 문서분류 결과가 나오면 계속 업데이트한다.

## 누가 사용한다?

거의 모든 agent가 사용한다.

* OCR Agent: 어떤 파일을 OCR할지 확인
* Classification Agent: 어떤 문서를 분류할지 확인
* Field Extraction Agent: 진단서와 의무기록을 찾음
* Policy Agent: 약관 문서를 찾음
* Report Agent: 어떤 문서를 근거로 썼는지 표시

## 비유

`document_manifest.json`은 **케이스 문서의 목차이자 출석부**야.

---

# 2. `classification_result.json`

## 한 줄 정의

**각 문서가 어떤 종류의 문서인지 분류한 결과표.**

## 왜 필요한가?

손해사정 케이스에는 다양한 문서가 섞여 있어.

* 진단서
* 의무기록
* 약관
* 보험증권
* 보험사 안내문
* 영상판독지
* 영수증
* 진료비 세부내역서

각 문서마다 뽑아야 할 정보가 다르기 때문에 먼저 문서 유형을 알아야 해.

예를 들어:

| 문서 유형   | 주로 뽑을 정보               |
| ------- | ---------------------- |
| 진단서     | 진단명, KCD, 치료기간, 병원명    |
| 의무기록    | 증상 경과, 치료 내용, 사고 이후 변화 |
| 약관      | 지급요건, 면책사항, 담보 조항      |
| 보험증권    | 가입 담보, 가입금액            |
| 보험사 안내문 | 감액사유, 부지급사유, 요청서류      |

## 담기는 내용

* 문서 ID
* 예측 문서 유형
* confidence
* 후보 문서 유형들
* 분류 근거 문장
* 사람이 검수해야 하는지 여부

## 예시

```json
{
  "case_id": "CASE_001",
  "documents": [
    {
      "document_id": "DOC_001",
      "predicted_document_type": "diagnosis_certificate",
      "document_type_label": "진단서",
      "confidence": 0.94,
      "candidate_types": [
        {
          "document_type": "diagnosis_certificate",
          "confidence": 0.94
        },
        {
          "document_type": "medical_record",
          "confidence": 0.31
        }
      ],
      "evidence_references": [
        {
          "page": 1,
          "quote": "진단서"
        },
        {
          "page": 1,
          "quote": "진단명: 요추 추간판탈출증"
        }
      ],
      "review_required": false
    },
    {
      "document_id": "DOC_006",
      "predicted_document_type": "insurer_response",
      "document_type_label": "보험사 안내문",
      "confidence": 0.86,
      "candidate_types": [
        {
          "document_type": "insurer_response",
          "confidence": 0.86
        },
        {
          "document_type": "insurance_certificate",
          "confidence": 0.22
        }
      ],
      "evidence_references": [
        {
          "page": 1,
          "quote": "보험금 지급 심사 결과 안내"
        }
      ],
      "review_required": true,
      "reviewer_role": "손해사정사"
    }
  ]
}
```

## 누가 만든다?

`DocumentPipelineAgent` 안의 `DocumentClassification` 단계가 만든다.

## 누가 사용한다?

* Field Extraction Agent: 진단서, 의무기록만 골라서 필드 추출
* Policy Agent: 약관 문서만 골라서 조항 추출
* Denial Reason Agent: 보험사 안내문만 골라서 감액사유 추출
* Screening Report Agent: 문서 구성 요약

## 비유

`classification_result.json`은 **각 문서에 이름표를 붙인 결과**야.

---

# 3. `extracted_claim_fields.json`

## 한 줄 정의

**청구 검토에 필요한 핵심 사실관계를 뽑아낸 구조화 결과.**

## 왜 필요한가?

손해사정사는 문서에서 핵심 사실을 먼저 확인해야 해.

예를 들면:

* 진단명은 무엇인가?
* KCD 코드는 무엇인가?
* 사고일은 언제인가?
* 발병일은 언제인가?
* 입원기간은 언제부터 언제까지인가?
* 수술명은 무엇인가?
* 치료기간은 어느 정도인가?
* 병원명은 어디인가?
* 사고 이후 증상 변화가 있는가?

이 정보를 agent가 한 곳에 모아야 이후 약관 매핑, 쟁점 분석, 리포트 생성이 가능해져.

## 담기는 내용

* 진단명
* KCD 코드
* 사고일
* 발병일
* 수술명
* 치료기간
* 입원기간
* 병원명
* 각 필드별 confidence
* 근거 문서와 페이지
* 검수 필요 여부

## 예시

```json
{
  "case_id": "CASE_001",
  "fields": {
    "diagnosis_name": {
      "value": "요추 추간판탈출증",
      "normalized_value": "lumbar_disc_herniation",
      "confidence": 0.88,
      "evidence_references": [
        {
          "document_id": "DOC_001",
          "document_type": "diagnosis_certificate",
          "page": 1,
          "quote": "진단명: 요추 추간판탈출증"
        }
      ],
      "review_required": false
    },
    "kcd_code": {
      "value": "M51.2",
      "confidence": 0.82,
      "evidence_references": [
        {
          "document_id": "DOC_001",
          "page": 1,
          "quote": "KCD: M51.2"
        }
      ],
      "review_required": false
    },
    "accident_date": {
      "value": "2024-03-12",
      "confidence": 0.72,
      "evidence_references": [
        {
          "document_id": "DOC_003",
          "document_type": "medical_record",
          "page": 2,
          "quote": "2024.03.12 사고 후 요통 발생"
        }
      ],
      "review_required": true,
      "reviewer_role": "손해사정사"
    },
    "admission_period": {
      "start_date": "2024-03-13",
      "end_date": "2024-03-20",
      "days": 8,
      "confidence": 0.86,
      "evidence_references": [
        {
          "document_id": "DOC_001",
          "page": 1,
          "quote": "입원기간: 2024.03.13 ~ 2024.03.20"
        }
      ],
      "review_required": false
    },
    "hospital_name": {
      "value": "[HOSPITAL_001]",
      "confidence": 0.91,
      "evidence_references": [
        {
          "document_id": "DOC_001",
          "page": 1,
          "quote": "발행 의료기관: [HOSPITAL_001]"
        }
      ],
      "review_required": false
    }
  }
}
```

## 누가 만든다?

`ClaimAnalysisAgent`의 `Claim Field Extraction` 단계가 만든다.

## 누가 사용한다?

* Coverage Identification: 어떤 담보가 관련 있는지 판단
* Case Type Classification: 후유장해, 실손, 수술비 등 사건 유형 분류
* Requirement Matching: 약관상 지급요건 충족 여부 후보 판단
* Screening Report Agent: 사건 개요 작성
* Draft Writer: 손사서 초안의 사건 개요 작성

## 비유

`extracted_claim_fields.json`은 **케이스의 핵심 사실 요약 카드**야.

---

# 4. `denial_reason_result.json`

## 한 줄 정의

**보험사의 반려·감액·부지급 사유를 추출하고 표준 taxonomy로 분류한 결과.**

## 왜 필요한가?

보험사 안내문에는 보통 이런 표현이 들어가.

* 기왕증 기여도 50% 적용
* 약관상 지급요건 미충족
* 치료 필요성 부족
* 제출서류 미비
* 면책사항 해당
* 장해율 과다
* 비급여 치료 적정성 부족

이 문장을 사람이 일일이 찾는 대신 AI가 추출해서 표준 코드로 정리한다.

## 담기는 내용

* 사유 ID
* 보험사 원문 표현
* 사유 요약
* taxonomy 코드
* taxonomy label
* 감액률
* 감액금액
* 요청서류
* 근거 문장
* confidence
* 검수 필요 여부

## taxonomy 예시

| 코드  | 의미              |
| --- | --------------- |
| R01 | 기왕증 / 기존 질환 기여도 |
| R02 | 장해율 과다          |
| R03 | 손해액 과다          |
| R04 | 약관상 지급요건 미충족    |
| R05 | 면책사항            |
| R06 | 치료 필요성 부족       |
| R07 | 과잉진료 / 비급여 적정성  |
| R08 | 서류 부족           |
| R09 | 동일 사유 재청구       |
| R99 | 기타 / 분류 불가      |

## 예시

```json
{
  "case_id": "CASE_001",
  "denial_reasons": [
    {
      "reason_id": "DR_001",
      "reason_type": "reduction",
      "taxonomy_code": "R01",
      "taxonomy_label": "기왕증 / 기존 질환 기여도",
      "raw_reason_text": "기왕증 기여도 50%를 적용하여 보험금을 감액합니다.",
      "insurer_claim_summary": "보험사는 기존 질환의 기여도를 이유로 보험금을 50% 감액한 것으로 보임",
      "reduction_rate": 0.5,
      "reduction_amount": null,
      "requested_documents": [],
      "confidence": 0.88,
      "evidence_references": [
        {
          "document_id": "DOC_006",
          "document_type": "insurer_response",
          "page": 1,
          "quote": "기왕증 기여도 50%"
        }
      ],
      "review_required": true,
      "reviewer_role": "손해사정사"
    },
    {
      "reason_id": "DR_002",
      "reason_type": "document_request",
      "taxonomy_code": "R08",
      "taxonomy_label": "서류 부족",
      "raw_reason_text": "추가 의무기록 제출이 필요합니다.",
      "insurer_claim_summary": "보험사가 추가 의무기록 제출을 요청함",
      "requested_documents": [
        "medical_record"
      ],
      "confidence": 0.84,
      "evidence_references": [
        {
          "document_id": "DOC_006",
          "page": 2,
          "quote": "추가 의무기록 제출이 필요합니다"
        }
      ],
      "review_required": true,
      "reviewer_role": "손해사정사"
    }
  ]
}
```

## 누가 만든다?

`DenialResponseAgent`가 만든다.
다만 PoC에서는 보험사 안내문이 처음부터 케이스 팩에 들어있을 수 있으므로, Phase 2 소속이지만 Week 2에 당겨 실행할 수 있어.

## 누가 사용한다?

* Screening Report Agent: “보험사 판단” 섹션 작성
* Policy-to-Denial Matching: 반려사유와 약관 조항 연결
* Evidence Validation: 보험사 주장과 기존 자료 비교
* Rebuttal Agent: 반박 포인트 생성
* Draft Writer: 손사서 초안의 감액/부지급 검토 섹션 작성

## 비유

`denial_reason_result.json`은 **보험사 주장의 구조화된 요약표**야.

---

# 5. `screening_report.json`

## 한 줄 정의

**앞 단계의 모든 결과를 모아 사람이 빠르게 검토할 수 있게 만든 1차 스크리닝 요약 결과.**

## 왜 필요한가?

개별 JSON들은 agent가 쓰기 좋은 구조야.
하지만 손해사정사나 팀원이 보기에는 흩어진 정보라서 불편해.

그래서 `screening_report.json`은 다음 내용을 한 번에 모아준다.

* 사건 개요
* 문서 구성
* 핵심 필드
* 청구담보
* 사건 유형
* 보험사 판단
* 핵심 쟁점
* 문서 간 불일치
* 추가 필요 서류
* 전문가 검수 포인트
* 손사서 초안 작성에 넘길 요약

그리고 이 JSON을 바탕으로 사람이 읽는 `screening_report.md`를 생성한다.

## 담기는 내용

* 사건 유형
* 주요 진단명
* 사고일 / 발병일
* 치료기간
* 청구담보
* 보험사 감액·부지급 사유
* 핵심 쟁점
* 불일치 사항
* 필요 서류
* 검수 포인트
* 전체 confidence
* 손사서 초안으로 넘길 요약

## 예시

```json
{
  "case_id": "CASE_001",
  "component": "ScreeningReportGeneration",
  "status": "success",
  "report_path": "outputs/CASE_001/screening_report.md",
  "case_summary": {
    "case_type": "후유장해",
    "main_diagnosis": "요추 추간판탈출증",
    "kcd_code": "M51.2",
    "accident_date": "2024-03-12",
    "treatment_period": {
      "start_date": "2024-03-13",
      "end_date": "2024-03-20"
    },
    "claim_coverages": [
      "상해후유장해"
    ]
  },
  "insurer_position": {
    "has_denial_or_reduction": true,
    "main_reason_code": "R01",
    "main_reason_label": "기왕증 / 기존 질환 기여도",
    "summary": "보험사는 기왕증 기여도 50%를 이유로 보험금을 감액한 것으로 보임"
  },
  "key_issues": [
    {
      "issue_id": "ISSUE_001",
      "title": "기왕증 기여도 적용의 적정성",
      "description": "의무기록상 사고 이후 증상 악화 정황이 있어 기왕증 기여도 50% 적용 비율은 추가 검토가 필요함",
      "related_documents": [
        "DOC_003",
        "DOC_006"
      ],
      "review_required": true,
      "reviewer_role": "의사"
    },
    {
      "issue_id": "ISSUE_002",
      "title": "상해후유장해 담보 지급요건 충족 여부",
      "description": "약관상 상해와 장해상태의 관련성 검토 필요",
      "related_documents": [
        "DOC_002",
        "DOC_004"
      ],
      "review_required": true,
      "reviewer_role": "손해사정사"
    }
  ],
  "inconsistencies": [
    {
      "field": "accident_date",
      "description": "의무기록과 보험사 안내문상 사고일이 다르게 기재됨",
      "severity": "medium",
      "review_required": true
    }
  ],
  "missing_documents": [
    {
      "document_type": "disability_certificate",
      "reason": "후유장해 여부 판단에 필요"
    }
  ],
  "review_points": [
    {
      "point": "기왕증 기여도와 사고 기여도 판단",
      "reviewer_role": "의사",
      "priority": "high"
    },
    {
      "point": "상해후유장해 담보 지급요건 충족 여부",
      "reviewer_role": "손해사정사",
      "priority": "high"
    }
  ],
  "draft_writer_input_summary": {
    "recommended_template_id": "draft_template_disability_v0.1",
    "sections_to_emphasize": [
      "사건 개요",
      "약관 검토",
      "의학적 검토",
      "감액사유 검토"
    ]
  },
  "confidence": 0.76,
  "review_required": true,
  "reviewer_role": "손해사정사"
}
```

## Markdown report 예시

`screening_report.json`은 machine-readable이고, `screening_report.md`는 사람이 읽기 위한 파일이야.

```markdown
# 스크리닝 리포트

## 1. 사건 개요
- 사건 유형: 후유장해
- 주요 진단명: 요추 추간판탈출증
- KCD 코드: M51.2
- 사고일: 2024-03-12
- 청구담보: 상해후유장해

## 2. 보험사 판단
- 감액 여부: 있음
- 감액사유: 기왕증 / 기존 질환 기여도
- 보험사 주장 요약: 기왕증 기여도 50%를 적용한 것으로 보임

## 3. 핵심 쟁점
1. 기왕증 기여도 적용의 적정성
2. 상해후유장해 담보 지급요건 충족 여부

## 4. 문서 간 불일치
- 사고일이 의무기록과 보험사 안내문에서 다르게 기재됨

## 5. 추가 필요 서류
- 후유장해진단서

## 6. 전문가 검수 포인트
- 의사 검수: 기왕증 기여도와 사고 기여도 판단
- 손해사정사 검수: 약관상 지급요건 충족 여부
```

## 누가 만든다?

`ReportGenerationAgent`가 만든다.

## 누가 사용한다?

* 사람 리뷰어
* Draft Writer Agent
* Evaluation Harness
* n8n/Slack 알림
* Notion DB 요약

## 비유

`screening_report.json`은 **케이스의 1차 브리핑 자료**야.

---

# 한눈에 보면

| 파일                            | 쉽게 말하면     | 주요 역할                       |
| ----------------------------- | ---------- | --------------------------- |
| `document_manifest.json`      | 문서 목록표     | 케이스 안에 어떤 문서가 있는지 관리        |
| `classification_result.json`  | 문서 이름표     | 각 문서가 진단서인지, 약관인지, 안내문인지 분류 |
| `extracted_claim_fields.json` | 핵심 사실 카드   | 진단명, KCD, 사고일, 치료기간 등 추출    |
| `denial_reason_result.json`   | 보험사 주장 요약표 | 감액·부지급 사유와 R코드 정리           |
| `screening_report.json`       | 1차 브리핑 자료  | 앞 결과들을 모아 손사 검토용 요약 생성      |

---

# 흐름으로 보면

```text
1. document_manifest.json
   → 이 케이스에 어떤 문서가 있는가?

2. classification_result.json
   → 각 문서는 무슨 종류인가?

3. extracted_claim_fields.json
   → 문서에서 핵심 사실은 무엇인가?

4. denial_reason_result.json
   → 보험사는 왜 감액/부지급/반려했는가?

5. screening_report.json
   → 그래서 이 케이스의 쟁점은 무엇이고, 사람이 무엇을 검토해야 하는가?
```

즉, 이 5개는 PoC에서 가장 중요한 **초기 스크리닝 backbone**이야.
