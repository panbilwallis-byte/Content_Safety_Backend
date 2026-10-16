# Content_Safety_Backend
# 本地启动
uv sync   安装依赖创建虚拟环境
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
d ~/yjp/项目名称
# 服务器首次启动

mkdir -p ./logs

nohup uv run uvicorn app.main:app \
  --host 127.0.0.1 \
  --port 8000 \
  >> ./logs/backend.log 2>&1 &

# 服务器二次启动
pkill -f 'uvicorn app.main:app' || true



nohup uv run uvicorn app.main:app \
  --host 127.0.0.1 \
  --port 8000 \
  >> ./logs/backend.log 2>&1 &