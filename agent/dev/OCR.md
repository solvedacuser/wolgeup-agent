# 원문 읽기·OCR 준비

기본 읽기는 페이지별로 품질 조건을 통과한 PDF 텍스트를 사용하고, 스캔·이미지 포함·불량 텍스트 페이지는 로컬 OCR로 전환합니다. 숫자나 날짜를 임의로 고치지 않으며 모델을 호출하지 않습니다. 검사를 통과한 텍스트도 원본 확인이 필요합니다.

## Windows 설치

Python 3.14·uv·7-Zip을 준비한 뒤 저장소 루트에서 실행합니다.

```powershell
uv sync --locked
powershell -File agent/dev/setup_ocr.ps1
```

스크립트는 고정 SHA-256으로 확인한 Tesseract 5.5.0과 `tessdata_fast` 4.1.0의 한국어·영어 데이터를 `tools/ocr/`에 설치합니다. 시스템 PATH는 바꾸지 않습니다. 설치물은 커밋하지 않습니다. Linux에서는 Tesseract와 `kor`, `eng` 언어 데이터를 별도로 설치해 PATH에 둡니다.

기존 설치를 쓰려면 `agent/.env`에 다음 값을 지정합니다.

```dotenv
AGENT_OCR_COMMAND=C:/path/to/tesseract.exe
AGENT_OCR_DATA_PATH=C:/path/to/tessdata
AGENT_FONT_PATH=C:/path/to/hangul-font.ttf
```

OCR은 `pypdfium2` 렌더링, 기본 300dpi, Tesseract `kor+eng`·`--psm 6`을 사용합니다. 디지털 텍스트만으로 읽을 수 있는 문서는 OCR 설치 없이 처리할 수 있습니다. OCR이 필요한 페이지에서 엔진·언어 데이터가 없으면 `OCR_UNAVAILABLE`로 중단합니다.

## 미리보기·보고서

원문 페이지 미리보기에는 Poppler의 `pdftoppm`이 필요합니다. PATH에 없으면 서버를 실행할 셸에 경로를 지정합니다.

```powershell
$env:AGENT_PDFTOPPM_PATH = 'C:/path/to/pdftoppm.exe'
uv run python -m agent.dev.test_server --port 4190
```

보고서는 `AGENT_FONT_PATH`의 사용 권한이 있는 한글 TrueType 글꼴을 사용합니다. Windows 기본값은 `C:/Windows/Fonts/malgun.ttf`이며 글꼴 파일은 저장소에 포함하지 않습니다.

AI의 API 키·모델은 `agent/.env.example`을 참고하세요. A는 저장 텍스트만 전송합니다. 선택 B는 원본 전체 PDF를 OpenAI에 보내는 별도 유료 실행이며 OCR 실패 시 자동 전환하지 않습니다.
