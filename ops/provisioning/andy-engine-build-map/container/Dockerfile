FROM python:3.13-alpine
WORKDIR /app
COPY server.py index.html app.js styles.css engine-map.json ./
RUN chmod 0444 /app/*
USER 10001:10001
ENV PYTHONDONTWRITEBYTECODE=1 ENGINE_MAP_LISTEN_ADDRESS=0.0.0.0 ENGINE_MAP_LISTEN_PORT=18120
CMD ["python", "-B", "/app/server.py"]
