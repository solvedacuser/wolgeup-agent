# 월급잇다 통합 백엔드

FastAPI 백엔드와 agent를 **Python 3.14 한 환경**에서 실행한다. 별도 agent HTTP 서버는 없다. Next.js는 별도 프로젝트에서 이 API를 사용한다. 이번 결과물에는 운영 프론트 화면을 포함하지 않는다.

## 설치와 실행

터미널의 현재 디렉터리를 이 `devmerge` 폴더로 설정한다.

```powershell
uv sync --locked
Copy-Item .env.example .env
uv run python -m app
```

API: `http://127.0.0.1:8000`, Swagger: `http://127.0.0.1:8000/docs`, 상태: `/health`.

- 디지털 PDF 읽기, 계산·대조, 템플릿 설명·질문·문의, PDF·ZIP은 모델 키 없이 실행한다.
- AI 추출 A, 선택적 PDF 보완 B, 자유 채팅에는 `.env`의 `OPENAI_API_KEY`가 필요하다. 키가 없으면 명시적 오류를 반환한다. 가상 고정 응답으로 대체하지 않는다.
- B는 사용자 선택 항목을 대상으로 **전체 PDF를 외부 모델에 전송**하는 별도 작업이다. 업로드·읽기·계산은 B를 자동 실행하지 않는다.
- 스캔 PDF/JPEG/PNG 읽기는 Tesseract 실행파일과 `kor`, `eng` 언어 데이터가 필요하다. `AGENT_OCR_COMMAND`, `AGENT_OCR_DATA_PATH`를 배포 환경에 맞게 지정한다. 이미지 원본과 파생 PDF를 별도로 보관한다.
- 한글 보고서 폰트는 `assets/fonts`에 라이선스와 함께 포함했다. `AGENT_FONT_PATH`로 다른 배포 폰트를 지정할 수 있다.
- 앱 데이터는 `DATA_DIR=./data` 아래에 새로 생성한다. 기존 원본 DB/환경파일을 사용하거나 복사하지 않는다.
- 단일 프로세스로 실행한다. 기본 `WORKER_CONCURRENCY=1`, 최대 4이며 사례/문서 범위의 중복 실행도 제한한다. `uvicorn --workers N` 다중 프로세스 배포는 지원하지 않는다.

## 처리 흐름

1. 회원가입·로그인 → 사업장과 급여기간 사례 생성 → 자료함 업로드/사례 연결.
2. 원문 읽기 작업 → 저장 읽기 확인 → 명시적 추출 A → 필요한 항목만 보완 B → 사용자 후보 선택/수정/보류.
3. 사례의 기본 또는 추가 비교 묶음에 문서 선택 → 확인 입력 저장 → analyze 요청.
4. 백엔드가 입력 스냅샷을 고정하고 `await run_task(...)` 실행 → 실행권·취소·소유권·삭제·버전 재검사 → 결과 저장.
5. 확정 결과를 선택해 설명·문의·보고서 생성 → 인증된 다운로드. 과거 결과 보고서는 당시 입력을 사용한다.

모든 임금 계산과 대조는 agent 규칙을 사용한다. 지원 범위는 표준 시급/월급 조건이며, 정보 부족·모름·보류·충돌을 0으로 계산하지 않는다. `COMPLETED`는 작업 저장 완료이지 모든 수당 계산 완료라는 뜻이 아니다.

## 검증

```powershell
uv run pytest tests/integration -q
uv run pytest agent/tests -q
uv run ruff check app agent tests/integration
```

Windows의 제한된 실행 샌드박스는 asyncio 내부 localhost 소켓을 차단할 수 있다. 이 경우 테스트가 이벤트 루프 생성에서 멈추므로 localhost 소켓을 허용하는 환경에서 실행한다.

실제 검증 결과·대역 범위·잔여 작업은 [검증 기록](docs/VERIFICATION.md), [요구사항 검토표](docs/REQUIREMENTS_MATRIX.md)를 확인한다. 실제 모델/OCR 품질, 실제 Next.js 화면, 광범위한 법률 적합성은 통합 테스트 통과와 별개다.

## 문서

- [폴더·호출·저장 구조](docs/ARCHITECTURE.md)

`agent/dev`와 그 화면 자산은 기존 회귀 테스트용으로만 보존했다. FastAPI는 이를 import하거나 서비스하지 않는다. 이전 백엔드의 숫자만 있는 수기/조건 입력은 자동으로 표준 임금 조건으로 승격하지 않으며, 비교 묶음의 명시적 확인 입력이 계산 실행 기준이다.
