# wolgeup-agent

계약서·근무기록·급여명세서·입금기록을 읽고, 사용자가 확인한 항목을 비교하는 Python 에이전트와 로컬 개발 화면입니다.

## 설치·실행

Python 3.14와 uv가 필요합니다. 저장소 루트에서 실행합니다.

```powershell
git clone https://github.com/solvedacuser/wolgeup-agent.git
cd wolgeup-agent
uv sync --locked
Copy-Item agent/.env.example agent/.env
# agent/.env에 OpenAI API 키와 모델 설정 입력
uv run python -m agent.dev.test_server --port 4190
```

[로컬 화면](http://127.0.0.1:4190)을 엽니다. 스캔 문서는 [OCR 설치 안내](agent/dev/OCR.md)에 따라 준비합니다. 원문 미리보기에는 Poppler, 한글 보고서에는 한글 TrueType 글꼴이 필요합니다.

## 사용 흐름

1. **원문 읽기:** 디지털 PDF 텍스트를 우선 사용하고 품질이 부족한 페이지는 로컬 OCR로 읽습니다.
2. **AI 처리 A:** 저장 텍스트로 유형·항목·표를 정리합니다. 오류 항목은 확인 필요로 남기고 정상 후보는 보존합니다.
3. **선택 보완 B:** 필요한 미확인 항목 1~50개를 선택해 PDF를 다시 판독합니다.
4. **문서 대조:** 원문을 보고 후보 선택·직접 수정·보류한 뒤, 확인한 자료를 독립 비교 묶음에서 대조합니다. 결과·설명·PDF/ZIP을 만들 수 있습니다.

A는 텍스트만 AI에 전송합니다. **B는 사용자가 별도로 실행할 때 원본 전체 PDF를 OpenAI에 전송하며 추가 비용이 발생합니다.** 선택 항목이나 페이지만 잘라 보내는 방식은 아닙니다. A/B·설명 생성은 유료 호출이며 원문 읽기·자료 대조는 모델을 호출하지 않습니다. 이미 시작한 요청은 취소해도 비용이 발생할 수 있습니다.

A/B 후보와 선택 이력을 따로 보존합니다. B 완료나 두 후보의 일치로 자동 확정하지 않으며, 정상 후보도 원문 확인 후 사용합니다. 기존 자료를 자동 재처리하지 않습니다.

## 범위·검사

단일 사용자·단일 프로세스용 개발 도구입니다. 운영 인증·사용자 격리·DB·작업 큐는 별도 구현이 필요합니다. 자료 간 단가·시간·명세서 합계·입금은 비교하지만 법정 임금 계산과 전체 미지급액 확정은 지원하지 않습니다.

```powershell
uv run pytest -q
uv run ruff check agent
uv run ruff format --check agent
```

일반 회귀 검사는 합성 자료와 가짜 공급자를 사용합니다. 원본 평가 자료가 필요한 검사는 자료가 없으면 건너뛰며, UI 회귀에는 Node.js가 필요합니다. 실제 AI 정확도 평가는 별도입니다.

API 키·업로드 문서·실행 결과는 커밋하지 않습니다. 로컬 결과는 `output/document-workspace/`에 저장됩니다.

[연결 계약](agent/README.md) · [화면·API](agent/dev/README.md) · [현재 구현 범위](agent/IMPLEMENTATION_STATUS.md)
