# wolgeup-agent

```powershell
git clone https://github.com/solvedacuser/wolgeup-agent.git
cd wolgeup-agent
uv sync
Copy-Item agent/.env.example agent/.env
notepad agent/.env
uv run python -m agent.dev.test_server --port 4190
# http://127.0.0.1:4190
```

```powershell
# Python OCR 설치 (선택 · 7-Zip 필요)
powershell -File agent/dev/setup_ocr.ps1
```
