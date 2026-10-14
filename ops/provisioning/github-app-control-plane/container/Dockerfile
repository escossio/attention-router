FROM python:3.13-alpine@sha256:2d9aefe2fef018a7eb2c13064c89c71929800fd2e5dccdbf52ea5da5bb8d929a
WORKDIR /app
COPY webhook_receiver.py /app/webhook_receiver.py
RUN chmod 0444 /app/webhook_receiver.py
USER 10001:10001
ENV PYTHONDONTWRITEBYTECODE=1 \
    GITHUB_WEBHOOK_HOST=0.0.0.0 \
    GITHUB_WEBHOOK_PORT=18104 \
    STATE_DIRECTORY=/state \
    CREDENTIALS_DIRECTORY=/run/webhook-credentials
CMD ["python", "-B", "-u", "/app/webhook_receiver.py"]
