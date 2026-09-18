# NOTE: this will not build successfully until mock_provider/app.py
# exists — see docs/CURSOR_CONTEXT.md §10. One generic file, run as
# four separate services (docker-compose.yml) with different
# SIMULATE_MODE / PROVIDER_NAME env vars.

FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
