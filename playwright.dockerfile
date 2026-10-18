# syntax=docker/dockerfile:1.7
# Both arguments are generated from the built Open WebUI image by the installer.
ARG PLAYWRIGHT_BASE_IMAGE
FROM ${PLAYWRIGHT_BASE_IMAGE}
ARG PLAYWRIGHT_VERSION
RUN python -m pip install --no-cache-dir "playwright==${PLAYWRIGHT_VERSION}" \
    && python -m playwright launch-server --help >/dev/null
COPY config/playwright/server.json /etc/playwright/server.json
COPY config/playwright/healthcheck.py /etc/playwright/healthcheck.py
# Browser profiles and downloads are disposable; never mount WebUI data here.
ENV HOME=/tmp/pwuser PYTHONDONTWRITEBYTECODE=1
USER pwuser
WORKDIR /tmp
ENTRYPOINT ["python", "-m", "playwright"]
CMD ["launch-server", "--browser", "chromium", "--config", "/etc/playwright/server.json"]
