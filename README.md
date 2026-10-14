# Content_Safety_Backend
cd ~/yjp/项目名称

pkill -f 'uvicorn app.main:app' || true
mkdir -p ./logs

nohup uv run uvicorn app.main:app \
  --host 127.0.0.1 \
  --port 8000 \
  >> ./logs/backend.log 2>&1 &