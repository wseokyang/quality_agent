# Replace the base with a CDEP approved image pinned by digest before production.
ARG PYTHON_BASE_IMAGE=python:3.12-slim
FROM ${PYTHON_BASE_IMAGE}

USER root

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    QUALITY_ARTIFACT_DIR=/var/lib/quality/artifacts

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.txt && \
    groupadd --gid 10001 quality && \
    useradd --uid 10001 --gid quality --no-create-home --shell /usr/sbin/nologin quality && \
    mkdir -p /var/lib/quality/artifacts && \
    chown -R 10001:10001 /var/lib/quality

COPY quality_api /app/quality_api
COPY migrations /app/migrations
COPY alembic.ini /app/alembic.ini

USER 10001:10001
EXPOSE 8000
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=3).read()"]
CMD ["uvicorn", "quality_api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
