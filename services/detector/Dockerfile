# Build context: repo root
# docker build -f services/detector/Dockerfile -t scarguard-detector .
FROM dustynv/l4t-pytorch:r36.4.0

# GStreamer + OpenGL deps required by OpenCV RTSP support on L4T
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgstreamer1.0-0 \
    libgstreamer-plugins-base1.0-0 \
    libgl1 \
    libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps before copying source (layer caching)
COPY services/detector/requirements.txt ./requirements.txt
# torch/torchvision/torchaudio are pre-installed in the base image;
# pip will skip reinstalling them if versions are compatible.
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY services/detector/src/ ./src/
COPY shared/ ./shared/

# Both src/ and shared/ on the path
ENV PYTHONPATH=/app/src:/app/shared
ENV PYTHONUNBUFFERED=1

CMD ["python", "src/main.py"]
