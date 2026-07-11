FROM python:3.12-slim

ARG SERVICE
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/server

WORKDIR /app
COPY server /app/server
RUN pip install --no-cache-dir -r "/app/server/${SERVICE}/requirements.txt"

EXPOSE 8070 8080 9090
