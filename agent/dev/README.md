# 로컬 문서 작업실

[설치·실행](../../README.md) 후 `http://127.0.0.1:4190`을 엽니다.

## 사용 순서

1. `/ocr`에서 PDF를 업로드하고 **원문 읽기**를 실행합니다. 디지털 텍스트 또는 로컬 OCR 결과를 저장합니다.
2. `/ai`에서 **AI 처리 A**를 실행합니다. 기본 `auto`가 문서 유형과 항목을 분류합니다.
3. 필요한 미확인 항목만 선택해 **PDF 보완 B**를 별도로 실행합니다. 원본 전체 PDF의 OpenAI 전송과 추가 비용이 발생합니다. A/B를 원문과 확인한 뒤 후보 선택·직접 수정·보류합니다.
4. `/compare`에서 자료와 기간을 골라 비교 묶음을 만듭니다. 같은 근로자·사업장인지, 시간 의미·상세 항목·입금 범위가 맞는지 확인합니다.
5. **확인한 값으로 비교**를 실행하고 값·차이·원문 근거를 확인합니다. 설명과 보고서는 별도로 실행합니다.

B는 기본 읽기와 A가 완료된 자료에서 실행합니다. B 완료나 A/B 일치는 자동 확정이 아닙니다. 같은 문서의 작업은 동시에 실행하지 않으며 취소·재개 시 누적 호출 예산을 유지합니다.

자료는 `output/document-workspace/`에 저장됩니다. 문서별 A/B·선택 이력과 비교 묶음은 분리되어 새로고침 후 복원됩니다. 후보 선택·재추출로 자료가 바뀌면 연결된 비교 묶음에서 다시 불러와 확인합니다. 비교용 수정은 원본 PDF를 바꾸지 않습니다.

## 핵심 API

변경 요청에는 `X-Local-Agent: 1`을 보냅니다. 업로드 본문은 `application/pdf`, 그 외 변경 본문은 JSON입니다.

| 요청 | 기능 |
| --- | --- |
| `GET /api/health`, `GET /api/documents` | 상태·업로드 목록 |
| `POST /api/uploads?document_type=auto` | PDF 업로드 |
| `GET /api/cases/{id}` | 저장 원문·A/B 후보·입력 |
| `POST /api/cases/{id}/jobs` | `ocr`, `extract`, `supplement` 등 작업 생성 |
| `POST /api/cases/{id}/candidate-selection` | 원문 확인 후 후보·직접 입력 선택 |
| `GET /api/jobs/{id}`, `POST /api/jobs/{id}/cancel` 또는 `/resume` | 작업 상태·취소·재개 |
| `GET /api/comparison-sources`, `GET /api/comparisons` | 비교 가능한 자료·묶음 목록 |
| `POST /api/comparisons`, `GET /api/comparisons/{id}` | 묶음 생성·조회 |
| `PUT /api/comparisons/{id}/materials` 또는 `/draft` | 연결 자료·수정 입력 저장 |
| `POST /api/comparisons/{id}/confirm` 또는 `/jobs` | 입력 확인·`analyze`, `explain`, `report` 실행 |
| `GET /api/artifacts/{id}` | 현재 결과 PDF·ZIP |

B 생성에는 `stage="supplement"`, `field_ids`, 현재 `extraction_revision`이 필요합니다. A의 `needs_review / missing` 중 미확정 항목 1~50개를 허용합니다. 후보 선택은 `field_id`, `candidate_id`, `source_confirmed=true`, 현재 버전을 요구합니다. 직접 입력의 `candidate_id`는 `manual`이며 `value`·`unit`을 함께 보냅니다.

새 비교 묶음은 `kind=comparison`, `document_id=null`이며 원문 읽기·추출 대상이 아닙니다. 기존 문서 사례의 확인·대조 API도 유지합니다. 운영 인증·동시성·자료 격리는 이 개발 서버의 제공 범위가 아닙니다.
