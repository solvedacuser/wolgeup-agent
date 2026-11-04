# 월급잇다 에이전트

Python 에이전트의 1차 구현입니다. 문서 추출·자료 대조·설명·상담 자료 생성과 백엔드 연결 계약을 제공합니다. 실제 백엔드·DB·작업 큐는 구현하지 않습니다.

W-01~W-05의 검증된 법정 임금 규칙은 등록되어 있지 않습니다. 이 항목과 R-07은 `needs_review`로 반환하고, `gross_total=null`, `scope_complete=false`, `release_ready=false`를 유지합니다. R-01~R-06은 필요한 자료가 확인된 범위에서 독립 실행합니다.

## 설치와 실행

저장소 루트에서 실행합니다. Python 3.14와 uv를 사용하며 의존성은 `uv.lock`에 고정되어 있습니다. 루트 Python 설정은 에이전트 개발용으로, 다른 담당자의 백엔드 기술을 지정하지 않습니다.

```powershell
uv sync --cache-dir .cache/uv
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m ruff check agent
.venv/Scripts/python.exe -m agent.dev.run_fixture demo --output output/agent-demo-0.1.0
```

데모는 합성 자료만 사용하고 API를 호출하지 않습니다. 분석 JSON·한글 요약 PDF·원본 포함 ZIP과 개발용 비공개 체크포인트를 출력합니다. 모든 실행 결과와 단계 기록의 `provider_mode`로 가상/실제 모델 모드를 구분합니다. 가상 포트와 데모의 결과 ID 부여는 운영 백엔드 구현의 대체물이 아닙니다.

Windows의 제한된 샌드박스가 asyncio 내부 로컬 소켓 생성을 막으면 이벤트 루프 시작에서 멈출 수 있습니다. 이 환경의 자동 검증은 해당 제한 밖에서 실행했습니다.

## 실제 모델 평가

`agent/.env.example`을 참고해 개발용 `agent/.env`에 키를 설정합니다. 실제 키는 버전 관리하지 않습니다. 기존 키를 소스·로그·결과 파일에 기록하지 않습니다. 추출·설명 모델은 각각 설정 가능하며 이번 검증은 `gpt-6-luna`로 수행했습니다.

```powershell
.venv/Scripts/python.exe -m agent.dev.evaluate_live --output output/live-evaluation
```

이 명령은 합성 텍스트 PDF 4종, 이미지로 만든 스캔 PDF 1종, 설명 1건을 실제 API로 처리하므로 비용이 발생합니다. 스키마·원문 인용·숫자 정규화를 검증하며 결과와 사용량을 `evaluation.json`에 기록합니다. 모델을 자동 변경하지 않습니다. 같은 출력 폴더로 재실행하면 완료 단계는 복구하고 같은 논리 작업의 호출 예산을 이어받습니다. 새 평가 캠페인은 새 출력 폴더를 사용합니다.

단일 샘플을 재검증할 수도 있습니다. 인덱스는 계약서 0, 근태 1, 명세서 2, 입금 3, 스캔 근태 4입니다.

```powershell
.venv/Scripts/python.exe -m agent.dev.evaluate_live --sample-index 2 --skip-explanation --output output/adapter-check
```

텍스트 PDF는 `pypdf`로 페이지 원문과 인용문을 대조합니다. 텍스트가 없는 페이지는 PDF 비전 입력을 사용하며 `vision_unverified`와 사용자 확인 필요 표시를 반환합니다. 스캔 인식 결과를 자동 원문 검증 완료로 표시하지 않습니다.

## 백엔드 연결

```python
from agent import run_task
from agent.config import AgentSettings
from agent.schemas.public.tasks import AgentTask

task = AgentTask.model_validate(request_data)
settings = AgentSettings(
    provider_mode="openai",
    openai_api_key=server_secret,
    extraction_model="gpt-6-luna",
    explanation_model="gpt-6-luna",
    model_config_version="luna-v1",
)
outcome = await run_task(task, ports=backend_ports, settings=settings)
```

이 코드는 백엔드가 실제 포트 구현·인증된 작업 문맥·고정 입력을 제공하는 위치에서 호출합니다. `AgentTask.model_validate`의 `ValidationError`는 호출자가 입력 오류로 처리해야 합니다. 검증 오류나 요청 전체를 로그에 그대로 남기지 않습니다. 유효한 봉투의 실행 실패는 `AgentOutcome.error`에 민감 자료 없는 코드로 반환합니다.

`completed`는 에이전트 처리 성공이며 영속 결과 저장 완료를 뜻하지 않습니다. 백엔드가 실행 소유권·취소·접근 철회·입력 버전을 원자적으로 재확인한 뒤 결과 ID·버전·최신성을 부여합니다. 확정 결과에는 `owner_id`, `case_id`, `workplace_id`를 포함하며 설명·보고서가 다른 소유자·사례 결과를 사용하지 못하게 검사합니다.

포트 구현은 [runtime.py](ports/runtime.py)의 비동기 Protocol을 따릅니다.

- 현재 실행과 접근 권한을 확인하고 철회·취소·일시 장애를 공개 예외로 변환합니다.
- 같은 작업·입력에 대한 체크포인트와 누적 모델 호출 예산을 영속 보존합니다. 모델 요청 전에 호출 시도를 저장합니다.
- 이전 실행 토큰의 체크포인트·파일·결과 저장을 거절합니다. 토큰은 로그에 남기지 않습니다.
- 원문을 읽을 때 실제 접근·문서 버전·해시를 검증합니다. 결과·보고서의 접근 철회는 빈 원본 목록을 받는 작업에서도 사례/결과 수준으로 확인해야 합니다.
- 생성 파일을 비공개로 보관하고 최종 확정 전에는 노출하지 않습니다. 원본 삭제·미확정 파일 정리·다운로드 시 접근 재검사는 백엔드 책임입니다.

`FakePorts`의 파일 저장은 단독 개발·단일 프로세스 테스트 전용입니다. 프로세스 간 잠금·운영 접근 통제·원자적 결과 확정은 구현하지 않으며 운영용으로 사용하지 않습니다. `shared/` 패키지나 에이전트의 DB 직접 접근은 없습니다.

## 공개 계약과 규칙

```powershell
.venv/Scripts/python.exe -m agent.dev.run_fixture schemas --output agent/contracts
.venv/Scripts/python.exe -m agent.dev.run_fixture check-release
```

두 번째 명령의 종료 코드 1은 현재 필수 계산 지원표가 미완료라는 정상적인 판정입니다. 공개 계약은 `agent/contracts`의 JSON Schema와 4종 작업의 정상·실패 예제를 참고합니다. 예제 실행 토큰은 마스킹된 자리표시자이므로 백엔드가 발급한 실행 토큰으로 교체합니다.

금액은 십진 문자열, 시간은 정수 분, 시각은 시간대 포함 값입니다. `known`과 미확인·모름·충돌을 구분합니다. 공개 모델은 임의의 추가 필드를 거절하며 `schema_version=1.0`을 지원합니다. 입력 스키마는 수기·문서 출처를 `SourceRef`로 통일하고 버전·승인 연결·용도를 함께 전달합니다.

`hashing.snapshot_hash`, `hashing.result_hash`를 사용해 내용을 고정합니다. 집합으로 정의된 입력 목록의 순서는 무시하고, 설명 문단처럼 순서가 의미 있는 자료는 순서를 보존합니다. 해시는 금액 표현을 정규화하고 실행 토큰·API 키를 포함하지 않습니다.

규칙은 `RuleRegistry`에 신뢰할 수 있는 애플리케이션 코드로 주입합니다. 공식 근거·독립 검증 사례·기간·검증 완료 상태가 없는 규칙은 등록할 수 없습니다. 사용자 JSON에서 임의 산식이나 코드를 실행하지 않습니다. 규칙 일부가 등록되어도 전체 임금·항목 대응 검증을 완료하기 전에는 총액이나 출시 준비 완료를 선언하지 않습니다.

## 보고서와 검증 기록

PDF에는 한글 TrueType 글꼴이 필요합니다. 기본값은 이 Windows 환경의 맑은 고딕이며 다른 환경은 `AgentSettings.font_path` 또는 개발 `.env`의 `AGENT_FONT_PATH`로 사용 권한이 있는 글꼴을 지정합니다. 글꼴이 없으면 깨진 PDF를 만들지 않고 실패합니다. 글꼴 파일 자체는 배포물에 복사하지 않았습니다.

검증 내역은 [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md)에 정리합니다. 실제 자료의 추출 품질·법정 임금 규칙·운영 백엔드의 동시성·삭제 검증은 별도 완료 조건입니다.
# 로컬 PDF 검증 화면

`python -m agent.dev.test_server --port 4190`으로 개발용 화면을 실행할 수 있습니다. 프로젝트 루트의 가상환경에서 실행하고 `http://127.0.0.1:4190`을 여세요. [준비·화면 사용법·로컬 API](dev/README.md)를 참고하세요.

검증 PDF 37건과 정답·평가 결과는 `output/contract-validation/`에 생성됩니다. 상세 평가는 `VALIDATION_REPORT.md`와 `document-results.csv`, `field-results.csv`에서 확인할 수 있습니다. 이 화면과 파일 저장소는 운영 백엔드 구현을 대체하지 않습니다.

