# Dockerfile for ci-proof engine (local build & zero-install container runner)
# Build locally: docker build -t ci-proof:local .
# Run:           docker run --rm -v "$(pwd):/workspace" ci-proof:local --version
FROM python:3.11-alpine

RUN apk add --no-cache git bash

WORKDIR /workspace

COPY pyproject.toml setup.py README.md /opt/ci-proof/
COPY engine/ /opt/ci-proof/engine/
COPY profiles/ /opt/ci-proof/profiles/
COPY scripts/ /opt/ci-proof/scripts/

RUN pip install --no-cache-dir "/opt/ci-proof[yaml]"

ENV PYTHONPATH="/opt/ci-proof"

ENTRYPOINT ["ci-proof"]
CMD ["--help"]
