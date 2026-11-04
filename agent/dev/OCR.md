# 로컬 한국어 OCR

```powershell
uv sync
powershell -File agent/dev/setup_ocr.ps1
```

Windows 준비 스크립트는 7-Zip이 필요합니다. Tesseract 5.5.0과 한국어·영어 언어 데이터를 프로젝트의 `tools/ocr/`에 내려받으며 고정 SHA256으로 확인합니다. 실행 파일·DLL·언어 데이터는 Git에 포함하지 않습니다.

다른 설치 경로는 `agent/.env`의 `AGENT_OCR_COMMAND`, `AGENT_OCR_DATA_PATH`로 지정합니다. Linux는 Tesseract 및 한국어·영어 데이터를 설치하고 PATH의 `tesseract`를 사용할 수 있습니다.

- [Tesseract 5.5.0](https://github.com/tesseract-ocr/tesseract/releases/tag/5.5.0)
- [tessdata_fast 4.1.0](https://github.com/tesseract-ocr/tessdata_fast/tree/4.1.0)

PDF를 `pypdfium2`로 렌더링한 뒤 로컬 OCR과 Python 규칙으로 읽습니다. OCR 추출에는 모델 호출이 없습니다. 원문 인식의 정확도는 사용자가 확인해야 합니다.
