# Build context: repo root
# docker build -f services/detector/Dockerfile -t scarguard-detector .
FROM dustynv/l4t-pytorch:r36.4.0@sha256:a05c85def9139c21014546451d3baab44052d7cabe854d937f163390bfd5201b

# GStreamer + OpenGL deps required by OpenCV RTSP support on L4T
# nvidia-l4t-tools provides tegrastats for Jetson GPU stats (aarch64 only).
# Use dpkg --print-architecture instead of ARG TARGETARCH: the latter is only
# auto-populated by `docker buildx build --platform`; plain `docker build` on
# a native arm64 host leaves it empty so the conditional would never fire.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgstreamer1.0-0 \
    libgstreamer-plugins-base1.0-0 \
    libgl1 \
    libglib2.0-0 \
    gosu \
 && if [ "$(dpkg --print-architecture)" = "arm64" ]; then \
        apt-get install -y --no-install-recommends nvidia-l4t-tools || \
        echo "nvidia-l4t-tools not available in apt repos; GPU stats will use sysfs fallback"; \
    fi \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps before copying source (layer caching)
COPY services/detector/requirements.txt ./requirements.txt
# torch/torchvision/torchaudio are pre-installed in the base image;
# pip will skip reinstalling them if versions are compatible.
RUN pip install --no-cache-dir --index-url https://pypi.org/simple/ -r requirements.txt

# Copy application source
COPY services/detector/src/ ./src/
COPY shared/ ./shared/

# Both src/ and shared/ on the path
ENV PYTHONPATH=/app/src:/app/shared
ENV PYTHONUNBUFFERED=1
ENV YOLO_CONFIG_DIR=/tmp/ultralytics

# Create non-root user for defense in depth.
# The video group provides GPU device access on most Linux hosts.
# Container starts as root; entrypoint fixes volume ownership then drops
# to scarguard via gosu.
RUN useradd -r -m -d /home/scarguard -s /usr/sbin/nologin -G video scarguard && \
    mkdir -p /data /config /models && \
    chown scarguard:scarguard /data /config /models

COPY services/detector/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python3", "src/main.py"]
