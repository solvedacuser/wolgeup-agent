# 월급잇다 에이전트

Python 에이전트의 1차 구현입니다. 문서 추출·자료 대조·설명·상담 자료 생성과 백엔드 연결 계약을 제공합니다. 실제 백엔드·DB·작업 큐는 구현하지 않습니다.



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
 저장소는 운영 백엔드 구현을 대체하지 않습니다.

