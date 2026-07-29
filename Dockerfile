FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

ARG APP_REVISION=unknown
ENV APP_REVISION=${APP_REVISION}

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    antiword \
    build-essential \
    curl \
    unar \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

COPY app ./app

CMD ["python", "-m", "app.healthcheck"]
