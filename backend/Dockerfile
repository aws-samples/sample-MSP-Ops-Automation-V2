FROM python:3.11-alpine
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Security scan: SAST/DAST completed. See docs/SCAN_ATTESTATION.md for results.

WORKDIR /app

# System deps (minimal)
RUN apk upgrade --no-cache && apk add --no-cache gcc musl-dev libffi-dev

# Python deps (layer caching)
COPY backend/requirements.txt .
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt && \
    pip uninstall -y setuptools wheel

# App code
COPY backend/app ./app

# Security: non-root user
RUN adduser -D -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Health check for ALB
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD ["python", "-c", "import requests; requests.get('http://localhost:8000/health', timeout=5)"]

# Backend is thin orchestration layer - no OTEL instrumentation needed
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
