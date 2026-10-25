FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install --no-cache-dir .

USER 10001:10001

VOLUME ["/config"]
ENTRYPOINT ["reolink-monitor"]
CMD ["--config", "/config/config.yaml"]
