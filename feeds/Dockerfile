FROM python:3.12-slim
WORKDIR /app
ENV PYTHONPATH=/app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY lifeboat/ lifeboat/
COPY feeds/ feeds/
EXPOSE 8080
CMD ["python", "-u", "-m", "feeds.handler"]

