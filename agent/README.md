# 에이전트 연결 안내

설치·화면 실행은 [저장소 안내](../README.md), 개발용 API는 [로컬 화면 안내](dev/README.md)를 참고하세요.

## 진입점

```python
from agent import run_task
from agent.schemas.public.tasks import AgentTask

outcome = await run_task(
    AgentTask.model_validate(request_data),
    ports=backend_ports,
    settings=settings,
)
```

`AgentSettings`에 모델·키·한도·글꼴을 주입합니다. 작업은 `extract_document`, `analyze_case`, `explain_result`, `build_report`입니다. 로컬 원문 읽기는 별도 `services.document_ocr.recognize_document`로 실행합니다.

[AgentPorts](ports/runtime.py)는 접근·취소 확인, 원문·저장 읽기 결과 제공, 체크포인트·누적 호출 예산·산출물 저장을 담당합니다. 호환 이름 `read_ocr`는 디지털 텍스트와 OCR을 모두 담은 저장 결과를 읽습니다. 문서 ID·버전·해시·읽기 결과 ID가 일치해야 합니다.

`completed`는 후보 처리 완료입니다. 운영 백엔드가 최종 접근·취소·입력 버전을 재확인하고 결과를 저장·확정해야 합니다. 로컬 파일 포트는 운영 권한과 동시성 보장을 대신하지 않습니다.

## 추출 계약

- 기본 `extraction_method=text`는 저장 텍스트로 A를 생성합니다. `pdf_supplement`는 선택한 `target_fields` 1~50개를 원본 PDF로 보완합니다. B의 전송·비용·확인 조건은 [저장소 안내](../README.md#사용-흐름)를 따릅니다.
- `raw_value`·`suggested_value`와 검사 통과값 `normalized_value`를 분리합니다. `validation_status`는 `valid / needs_review / missing`이며 문제 항목의 정규화 값은 null입니다. 항목 오류는 다른 정상 후보를 폐기하지 않습니다.
- `source_method`는 `pdf_text / python_ocr / mixed / pdf_supplement`, `page_methods`는 페이지별 읽기 방법입니다. `text_match / ocr_unverified / vision_unverified`는 근거의 종류이며 원본 확인 완료가 아닙니다.
- 원본 해시·A 버전·읽기 결과를 고정해 A/B와 사용자 선택을 보존합니다. B 완료는 자동 적용이 아니며 선택 후 연결된 비교 자료를 다시 확인합니다.

현재 엔진은 `agent-0.3.0`, 공개 스키마·체크포인트는 `1.0`, 추출 계약 기본값은 `1.2`입니다. 이전 `1.0 / 1.1` 추출 자료를 호환해 읽으며 자동 재처리하지 않습니다.

공개 [JSON Schema](contracts/)와 [요청·응답 예제](tests/contracts/)를 제공합니다. 스키마 생성은 모델을 호출하지 않습니다.

```powershell
uv run python -m agent.export_contracts --output agent/contracts
```

금액은 십진 문자열, 시간은 정수 분으로 전달합니다. 미확인 값은 0과 구분합니다. 현재 법정 임금 규칙 W-01~05와 R-07은 미완료이며 `gross_total=null`, `scope_complete=false`, `release_ready=false`를 유지합니다.
