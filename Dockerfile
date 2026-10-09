# Pitlane container image (OPS-6). Includes ffmpeg.
# Multi-arch (NFR-7): docker buildx build --platform linux/amd64,linux/arm64 -t pitlane:latest .
# Debian trixie ships ffmpeg 7.x (SPEC §4 requires ffmpeg >= 6).
FROM python:3.12-slim-trixie

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --system --uid 1000 --home /data pitlane \
 && mkdir -p /config /data /inbox /library \
 && chown pitlane /data /inbox /library

ENV PITLANE_CONFIG=/config/pitlane.yaml \
    PITLANE_LOG_FORMAT=json \
    PYTHONUNBUFFERED=1

USER pitlane
EXPOSE 5004
VOLUME ["/config", "/data"]
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:5004/healthz', timeout=4)" || exit 1

# tini reaps children and forwards signals (SIGHUP reloads the config).
ENTRYPOINT ["tini", "--", "pitlane"]
CMD ["run"]
