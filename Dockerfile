FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY monitor.py .

RUN useradd --uid 1000 --create-home monitor \
    && mkdir -p /data && chown monitor /data
USER monitor
VOLUME ["/data"]

ENTRYPOINT ["python", "monitor.py"]
