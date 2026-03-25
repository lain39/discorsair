# syntax=docker/dockerfile:1.7

FROM ghcr.io/flaresolverr/flaresolverr:v3.4.5

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    HOST=127.0.0.1 \
    PORT=8191

USER root

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m pip install --no-cache-dir uv

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY plugins/sample_forum_ops ./plugins/sample_forum_ops

RUN uv sync --frozen --no-dev --python "$(command -v python3)"

COPY config/app.json.template ./config/app.json.template
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && mkdir -p /app/config /data /data/locks \
    && chmod 0777 /app/config /data /data/locks

USER flaresolverr

VOLUME ["/data"]

EXPOSE 17880

CMD ["serve"]

ENTRYPOINT ["docker-entrypoint.sh"]
