FROM python:3.12-slim
RUN pip install --no-cache-dir flask gunicorn prometheus-client
COPY app.py /app/app.py
WORKDIR /app
CMD ["gunicorn", "-b", "0.0.0.0:8000", "-w", "1", "--threads", "4", "app:app"]
