FROM python:3.12-slim
WORKDIR /app
ENV PYTHONPATH=/app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY lifeboat/ lifeboat/
COPY feeds/ feeds/
# scenarios for `make replay` (the replay Job runs from this image)
COPY data/ data/
CMD ["kopf", "run", "--standalone", "--namespace", "lifeboat", \
     "--liveness=http://0.0.0.0:8080/healthz", "-m", "lifeboat.operator"]
