# 월급잇다 통합 백엔드 (devmerge)

`feat/devmerge-integration` 브랜치의 통합 실행 대상은 `devmerge/` 임

FastAPI 백엔드가 에이전트를 Python 함수로 직접 호출하며, Python 3.14 한 환경에서 실행합니다. Next.js 프론트엔드는 별도 프로젝트에서 이 API를 사용합니다.

## 폴더 구분

| 경로 | 역할 |
| --- | --- |
| `agent/` | 기존 독립 에이전트와 로컬 개발 화면 보존본 |
| `devmerge/app/` | FastAPI API, 인증, DB, 작업 관리와 에이전트 연결 |
| `devmerge/agent/` | 통합본에서 사용하는 추출·계산·대조·설명·보고서 처리 모듈 |
| `devmerge/docs/` | 통합 구조, 요구사항 검토와 검증 기록 |

**설치·실행·테스트는 모두 `devmerge/`에서 진행합니다.**

통합본은 루트 `agent/`를 참조하지 않습니다. `agent.dev.test_server`는 기존 개발 화면용이며, 통합 API 실행 명령은 `python -m app`입니다.

## 설치·실행 — Windows PowerShell

Git과 [uv](https://docs.astral.sh/uv/getting-started/installation/)를 설치한 뒤 실행합니다.

```powershell
git clone --branch feat/devmerge-integration https://github.com/solvedacuser/wolgeup-agent.git
cd wolgeup-agent/devmerge

uv python install 3.14
uv sync --locked
Copy-Item .env.example .env

# .env에서 API 키와 필요한 설정을 입력한 뒤 실행
uv run python -m app
```

이미 저장소를 받았다면 해당 브랜치의 `devmerge/`로 이동합니다. `.env` 복사는 최초 1회만 수행하며, 기존 설정이 있으면 그대로 수정합니다.

| 접속 주소 | 용도 |
| --- | --- |
| http://127.0.0.1:8000/docs | Swagger API 문서와 요청 테스트 |
| http://127.0.0.1:8000/health | 서버 상태 확인 |
| http://127.0.0.1:8000/openapi.json | OpenAPI 명세 |

## 환경 설정

설정 파일은 **`devmerge/.env`**입니다. 루트 `agent/.env`와 구분합니다.

| 설정 | 용도 |
| --- | --- |
| `OPENAI_API_KEY` | AI 추출 A, 선택적 PDF 보완 B, 자유 채팅에 필요한 키 |
| `AGENT_OCR_COMMAND` | Tesseract 실행 명령 또는 실행파일 경로 |
| `AGENT_OCR_DATA_PATH` | `kor.traineddata`와 `eng.traineddata`가 있는 언어 데이터 폴더 |
| `AGENT_FONT_PATH` | 한글 보고서 생성에 사용할 폰트 경로 |
| `DATA_DIR` | DB·업로드·산출물 저장 위치. 기본값 `./data` |
| `CORS_ORIGINS` | API 접근을 허용할 프론트엔드 주소의 JSON 배열 |
| `WORKER_CONCURRENCY` | 작업 동시 실행 수. 기본값 1, 최대 4 |

디지털 PDF 읽기, 계산·대조, 템플릿 설명·질문·문의, PDF·ZIP 생성은 모델 키 없이 실행합니다. AI 추출·PDF 보완·자유 채팅은 API 키가 필요하며, 키가 없으면 오류를 반환합니다.

스캔 PDF·JPEG·PNG 읽기에는 [Tesseract](https://tesseract-ocr.github.io/tessdoc/Installation.html)와 한국어·영어 언어 데이터가 필요합니다. 예제의 `AGENT_OCR_DATA_PATH=assets/tessdata`는 실제 언어 데이터 설치 경로로 수정해야 합니다.

한글 보고서 폰트는 `devmerge/assets/fonts/`에 포함되어 있습니다.

## 처리 흐름

1. 회원가입·로그인 후 사업장과 급여기간 사례를 생성합니다.
2. 문서를 업로드·연결하고 원문 읽기 결과를 저장합니다.
3. AI 추출 A를 실행하고, 필요한 항목만 PDF 보완 B로 처리합니다.
4. 사용자가 후보를 선택·수정·보류하고, 비교 묶음별 문서와 확인 입력을 저장합니다.
5. 분석을 요청하면 백엔드가 입력 스냅샷을 고정하고 에이전트 함수를 실행합니다.
6. 확정 결과를 기준으로 설명·회사 문의·PDF·선택 원본 ZIP을 생성합니다.

PDF 보완 B는 사용자가 선택한 항목을 확인하기 위해 **전체 PDF를 외부 모델에 전송하는 별도 작업**입니다. 업로드·원문 읽기·계산 과정에서 자동 실행하지 않습니다.

A/B 추출 결과는 후보이며, 사용자 확인 후 계산 입력으로 사용합니다.

## 백엔드와 에이전트 연결

백엔드의 비동기 작업 처리 코드에서 에이전트를 직접 호출합니다.

```python
from agent import run_task

outcome = await run_task(
    task,
    ports=ports,
    settings=agent_settings,
)
```

| 항목 | 내용 |
| --- | --- |
| `task` | 작업 종류와 입력을 담은 `AgentTask` |
| `ports` | 자료 읽기·권한 확인·체크포인트·산출물 저장 기능을 제공하는 객체 |
| `agent_settings` | 모델·시간 제한·호출 예산 등을 담은 `AgentSettings` |
| `outcome` | 완료·실패·취소 상태와 작업 결과를 담은 `AgentOutcome` |

에이전트 핵심 모듈은 FastAPI나 백엔드 DB를 직접 사용하지 않습니다.

- `app/agent_ports.py`: 백엔드의 DB·파일 접근 구현
- `app/agent_service.py`: 작업 실행과 최종 결과 확정
- `agent/runner.py`: 에이전트 작업 실행
- `agent/runtime.py`: 실행 상태·체크포인트·AI 호출 관리

별도 에이전트 HTTP 서버는 필요하지 않습니다.

연결 규격과 예제:

- [백엔드 연결 계약](devmerge/agent/README.md#백엔드-연결-계약)
- [입력 스키마](devmerge/agent/contracts/AgentTask.schema.json)
- [출력 스키마](devmerge/agent/contracts/AgentOutcome.schema.json)
- [요청·응답 예제](devmerge/agent/tests/contracts/)

## 검증

`devmerge/`에서 실행합니다.

```powershell
uv run pytest -q
uv run ruff check app agent tests/integration tests/backend/test_retained.py
```

실제 모델·스캔 OCR 품질 검증과 자동 테스트의 범위는 구분합니다. 검증 결과와 남은 작업은 다음 문서에서 확인합니다.

- [통합 구조와 저장·호출 흐름](devmerge/docs/ARCHITECTURE.md)
- [검증 기록](devmerge/docs/VERIFICATION.md)
- [요구사항 검토표](devmerge/docs/REQUIREMENTS_MATRIX.md)
- [임금 계산 지원 범위](devmerge/agent/PAYROLL.md)

## 지원 범위

- 단일 서버 프로세스를 지원합니다. Uvicorn 다중 프로세스 실행은 지원하지 않습니다.
- 임금 계산은 표준 시급·월급 조건을 지원합니다.
- 정보 부족·모름·보류·충돌을 0으로 계산하지 않습니다.
- 작업의 `COMPLETED` 상태는 결과 저장 완료를 뜻하며, 모든 임금 항목의 계산 완료를 뜻하지 않습니다.
- 운영 프론트엔드 화면은 포함하지 않습니다.

## Linux — 핵심 설치·실행

Git과 `uv` 설치 후 실행합니다. `.env` 복사는 최초 1회만 수행합니다.

```bash
git clone --branch feat/devmerge-integration https://github.com/solvedacuser/wolgeup-agent.git
cd wolgeup-agent/devmerge

uv python install 3.14
uv sync --locked
cp .env.example .env

# .env 설정 후 실행
uv run python -m app
```

스캔 문서를 읽는 경우 Ubuntu/Debian에서는 OCR도 설치합니다.

```bash
sudo apt-get update
sudo apt-get install -y tesseract-ocr tesseract-ocr-kor tesseract-ocr-eng
tesseract --list-langs
```

`.env`의 `AGENT_OCR_COMMAND`는 `tesseract`로 설정하고, `AGENT_OCR_DATA_PATH`는 위 명령에 표시되는 언어 데이터 경로로 변경합니다. `kor`·`eng`가 모두 표시되는지 확인합니다.

실행 후 API 문서: http://127.0.0.1:8000/docs
