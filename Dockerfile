FROM python:3.12-slim

WORKDIR /app

# Install dependencies first for better layer caching.
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# App code: backend package + static frontend. main.py locates the frontend
# relative to its own path (/app/backend/app/main.py -> /app/frontend).
COPY backend/ backend/
COPY frontend/ frontend/

ENV ANTHEM_PORT=14999 \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# --app-dir puts /app/backend on sys.path so "app.main:app" resolves.
CMD ["uvicorn", "app.main:app", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8000"]
