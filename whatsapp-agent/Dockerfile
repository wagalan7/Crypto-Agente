FROM python:3.11-slim
# build: 2026-06-30-01

WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ .

RUN mkdir -p /data
ENV DATA_DIR=/data

EXPOSE 8001

CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8001}
