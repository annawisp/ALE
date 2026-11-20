FROM node:22-bookworm-slim
ARG CODEX_VERSION=0.169.0
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-openpyxl python3-pypdf2 poppler-utils git ca-certificates && rm -rf /var/lib/apt/lists/* && npm install --global @openai/codex@${CODEX_VERSION} && codex --version
COPY worker.py /opt/worker.py
COPY marketplace.json /catalog/.agents/plugins/marketplace.json
RUN mkdir -p /exam/input /exam/out /catalog/plugins/accounting /relay
ENV HOME=/run/exam-home CODEX_HOME=/run/exam-home/.codex
WORKDIR /exam
ENTRYPOINT ["python3", "/opt/worker.py"]
