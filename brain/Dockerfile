# City Brain image: every Brain module + the mocks, started by run.py.
# Secrets are mounted at run time (never baked in): see docker-compose.yml at the repo root.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONIOENCODING=utf-8
WORKDIR /app/brain

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN useradd --create-home --uid 10001 brain && mkdir -p /app/logs logs && chown -R brain /app/logs logs
USER brain

# Docker overlay: broker host = the compose service, Ollama on the host machine, servers on 0.0.0.0
ENV BRAIN_CONFIG_OVERLAY=config/overlays/docker
EXPOSE 8090 8080
CMD ["python", "run.py", "--mocks"]
