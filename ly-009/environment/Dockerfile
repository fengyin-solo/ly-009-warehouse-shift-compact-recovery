FROM python:3.13.15-slim-bookworm@sha256:00faa2debb87529f9f0764e9491d8ba400a3678976616c3bd7cb193745ac20d1

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY app/ /app/

RUN mkdir -p /logs/verifier \
    && chmod 0777 /app/data \
    && chmod 0755 /app/warehouse_policy.py
