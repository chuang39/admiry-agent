# agent/Dockerfile -- the Admiry marketing agent (FastAPI + FastMCP, spec 2026-09-24 §4.6).
# Build from the repo root: docker buildx build --platform linux/amd64 -f agent/Dockerfile .
FROM python:3.12-slim
WORKDIR /app
COPY agent/requirements.txt /app/agent/requirements.txt
RUN pip install --no-cache-dir -r /app/agent/requirements.txt
COPY agent/ /app/agent/
ARG BIDX_BUILD_SHA=dev
ENV BIDX_BUILD_SHA=${BIDX_BUILD_SHA} PORT=8097 PYTHONUNBUFFERED=1
EXPOSE 8097
CMD ["python", "-m", "agent"]
