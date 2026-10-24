ARG NODE_BASE_IMAGE=node:22-alpine
ARG PYTHON_BASE_IMAGE=python:3.12-alpine3.22
FROM ${NODE_BASE_IMAGE} AS signal-control-build

ARG NPM_REGISTRY_URL=https://registry.npmjs.org/
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm config set registry "${NPM_REGISTRY_URL}" \
    && npm config set replace-registry-host always \
    && npm config set strict-ssl true \
    && npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM ${PYTHON_BASE_IMAGE}
ARG PIP_INDEX_URL=https://pypi.org/simple/
ENV TZ=UTC PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN apk --no-cache --upgrade add \
    bash ca-certificates curl git gcc musl-dev libffi-dev
WORKDIR /home/devsec/app
COPY requirements.txt .
RUN pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" -r requirements.txt
COPY . .
COPY --from=signal-control-build /app/static/next ./app/static/next
RUN addgroup --system devsec && adduser --system --ingroup devsec devsec \
    && chown -R devsec:devsec /home/devsec/app \
    && mkdir -p /data/iac_reports /data/config_scan_reports \
    && chown -R devsec:devsec /data
USER devsec
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
