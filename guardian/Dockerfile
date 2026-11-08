FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 GUARDIAN_ENV_FILE=/nonexistent
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src ./src
COPY mocks ./mocks
COPY tests ./tests
RUN useradd -r -u 10001 guardian && mkdir -p /keys && chown guardian /keys
USER guardian
EXPOSE 8443
HEALTHCHECK --interval=10s --timeout=3s --retries=6 CMD python -c "import ssl,urllib.request,os;c=ssl.create_default_context(cafile=os.environ['TLS_CA_FILE']);c.load_cert_chain('/certs/ui.pem','/certs/ui.key');urllib.request.urlopen('https://localhost:'+os.environ.get('API_PORT','8443')+'/healthz',context=c)"
CMD ["python", "-m", "src.main"]
