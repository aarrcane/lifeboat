FROM python:3.12-slim
RUN pip install --no-cache-dir boto3
COPY lifeboat/checkpoint.py /app/checkpoint.py
COPY workloads/worker/worker.py /app/worker.py
WORKDIR /app
CMD ["python", "-u", "worker.py"]
