FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends git curl unzip && rm -rf /var/lib/apt/lists/*

# Install 1Password CLI (multi-arch)
ARG TARGETARCH
RUN case "${TARGETARCH}" in \
        arm64) OP_ARCH="arm64" ;; \
        amd64) OP_ARCH="amd64" ;; \
        *)     OP_ARCH="amd64" ;; \
    esac && \
    curl -sSfo /tmp/op.zip "https://cache.agilebits.com/dist/1P/op2/pkg/v2.30.3/op_linux_${OP_ARCH}_v2.30.3.zip" \
    && unzip -o /tmp/op.zip -d /usr/local/bin/ op \
    && rm /tmp/op.zip \
    && chmod +x /usr/local/bin/op

COPY --from=ghcr.io/astral-sh/uv:0.7 /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

WORKDIR /app

# 1. Install core dependencies (cached unless pyproject.toml/uv.lock change)
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# 2. Install project with source
COPY src/ src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# 3. Install tool dependencies via bind mount (doesn't create a layer from tools/)
#    The uv cache mount means even on rebuild this is fast.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=tools,target=/tmp/tools \
    python -c "\
import tomllib, pathlib; \
deps = set(); \
[deps.update(tomllib.load(open(p,'rb')).get('project',{}).get('dependencies',[])) \
 for p in pathlib.Path('/tmp/tools').glob('*/pyproject.toml')]; \
open('/tmp/pd.txt','w').write('\n'.join(sorted(deps)))" \
    && uv pip install -r /tmp/pd.txt --quiet \
    && rm /tmp/pd.txt

# 4. Copy tool source
COPY tools/ tools/

# Copy migrations
COPY migrations/ migrations/

# Entrypoint: 1Password bootstrap (signin → load secrets → signout → exec)
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uv", "run", "uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8000"]
