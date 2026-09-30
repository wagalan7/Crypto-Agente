FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Verify async DB dependencies during the build, without opening a connection.
RUN python -c "import asyncio; from sqlalchemy.ext.asyncio import create_async_engine; from sqlalchemy.util.concurrency import greenlet_spawn; asyncio.run(greenlet_spawn(lambda: None)); create_async_engine('postgresql+asyncpg://localhost/build_check')"

COPY . .

EXPOSE 8000

CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
