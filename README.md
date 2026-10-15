# Content_Safety_Backend
# 第一次启动、
uv sync   安装依赖
uv run  uvicorn app:main --host 127.0.0.1 --port 8000
d ~/yjp/项目名称
# 重新启动
pkill -f 'uvicorn app.main:app' || true
mkdir -p ./logs

nohup uv run uvicorn app.main:app \
  --host 127.0.0.1 \
  --port 8000 \
  >> ./logs/backend.log 2>&1 &