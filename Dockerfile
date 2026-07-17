FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data

ENV MEMEX_DB_PATH=/app/data/memex.db
ENV LITELLM_BASE_URL=http://host.docker.internal:4000
ENV LITELLM_API_KEY=sk-1234
ENV INNER_MODEL=claude-3-haiku-20240307
ENV EMBED_MODEL=text-embedding-3-small
ENV PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD curl -f http://localhost:8000/health || exit 1

CMD ["sh", "-c", "uvicorn api.main:app --host 0.0.0.0 --port ${PORT} --workers 2 --forwarded-allow-ips='*'"]

