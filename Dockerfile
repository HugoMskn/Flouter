FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY static ./static

RUN mkdir -p /app/data && chown -R 1000:1000 /app

USER 1000:1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/app/data \
    HOME=/tmp

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--no-access-log", "--log-level", "critical", "--timeout-keep-alive", "30"]
