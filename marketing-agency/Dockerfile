FROM node:20-slim AS frontend-builder
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm install
COPY frontend/ ./
RUN npm run build

FROM python:3.11-slim
WORKDIR /app

RUN mkdir -p /data

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ .

COPY --from=frontend-builder /app/frontend/dist ./static

EXPOSE 8000
ENV DATABASE_URL=sqlite:////data/content_agency.db
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
