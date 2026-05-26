# Build context: repo root
# docker build -f services/trainer/Dockerfile -t scarguard-trainer .
FROM dustynv/l4t-pytorch:r36.4.0@sha256:a05c85def9139c21014546451d3baab44052d7cabe854d937f163390bfd5201b

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgstreamer1.0-0 \
    libgstreamer-plugins-base1.0-0 \
    libgl1 \
    libglib2.0-0 \
    gosu \
    ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY services/trainer/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --index-url https://pypi.org/simple/ -r requirements.txt

COPY services/trainer/src/ ./src/
COPY shared/ ./shared/
COPY training/prepare_dataset.py ./scripts/prepare_dataset.py
COPY training/train.py ./scripts/train.py

ENV PYTHONPATH=/app/src:/app/shared
ENV PYTHONUNBUFFERED=1
ENV YOLO_CONFIG_DIR=/tmp/ultralytics

RUN useradd -r -m -d /home/scarguard -s /usr/sbin/nologin -G video scarguard && \
    mkdir -p /data /config /models /tmp/ultralytics /data/training_uploads /data/training_workspace && \
    chown scarguard:scarguard /data /config /models /tmp/ultralytics

COPY services/trainer/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python3", "src/main.py"]
