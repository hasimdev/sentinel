# ShopLite release image. Built from a Git tag by `make release VERSION=x.y.z`,
# which passes the version and commit so every log line and metric carries them.
FROM python:3.12.11-slim

WORKDIR /app
COPY apps/shoplite/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt && useradd --create-home app

COPY common /app/common
COPY apps/__init__.py /app/apps/__init__.py
COPY apps/shoplite /app/apps/shoplite

ARG VERSION=0.0.0
ARG COMMIT_SHA=unknown
ENV SERVICE=shoplite \
    VERSION=${VERSION} \
    COMMIT_SHA=${COMMIT_SHA} \
    PYTHONUNBUFFERED=1

USER app
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "apps.shoplite.main:app", "--host", "0.0.0.0", "--port", "8000"]
