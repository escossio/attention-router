FROM python:3.13-slim
RUN pip install --no-cache-dir "psycopg[binary]==3.2.10"
WORKDIR /app
COPY reader.py flow_map.py ./
COPY map/adapter.py ./
RUN chmod 0644 /app/reader.py /app/flow_map.py /app/adapter.py
USER 18089:18089
EXPOSE 8080
CMD ["python", "-u", "adapter.py"]
