# sentinel/deployer: the deploy code from this folder, packaged so the Harness delegate can run
# exactly what `make deploy` runs. Build with `make deployer`.
# It talks to Docker through the mounted docker.sock, so it runs as root (like the docker CLI).
FROM docker:27.5.1-cli AS docker-cli

FROM python:3.12.11-slim
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
RUN pip install --no-cache-dir httpx2==2.13.1
WORKDIR /app
COPY deploy/__init__.py deploy/shoplite.py /app/deploy/
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "-m", "deploy.shoplite"]
