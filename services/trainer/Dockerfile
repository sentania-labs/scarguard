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
# Note: dustynv/l4t-pytorch base image ships two pre-existing pip check warnings/errors:
# pygobject requiring pycairo and ninja platform mismatch. Ignore these known base image defects.
RUN pip check | grep -vE 'pygobject 3\.42\.1 requires pycairo, which is not installed\.|ninja 1\.11\.1\.1 is not supported on this platform|No broken requirements found\.' | { ! grep .; } && \
    python3 -c "import importlib.metadata as m, torch, torchvision; assert torch.__version__.startswith('2.4.0'); assert torch.version.cuda == '12.6'; assert m.version('ultralytics') == '8.4.56'; print(torch.__version__, torch.version.cuda, torchvision.__version__, m.version('ultralytics'))"
RUN pip freeze --all | LC_ALL=C sort > /app/training-stack.freeze && \
    dpkg-query -W -f='${Package}=${Version}\n' | LC_ALL=C sort > /app/os-packages.manifest

COPY services/trainer/src/ ./src/
COPY shared/ ./shared/
COPY training/prepare_dataset.py ./scripts/prepare_dataset.py
COPY training/train.py ./scripts/train.py
COPY training/nvml_compat.py ./scripts/nvml_compat.py

ARG VERSION=local-dev
ARG GIT_COMMIT=unknown
ARG BUILD_DATE=unknown
LABEL org.opencontainers.image.version=${VERSION} \
      org.opencontainers.image.revision=${GIT_COMMIT} \
      org.opencontainers.image.created=${BUILD_DATE} \
      org.opencontainers.image.base.name="dustynv/l4t-pytorch:r36.4.0@sha256:a05c85def9139c21014546451d3baab44052d7cabe854d937f163390bfd5201b" \
      org.scarguard.training.stack="torch=2.4.0,cuda=12.6,ultralytics=8.4.56" \
      org.scarguard.training.python-manifest="/app/training-stack.freeze" \
      org.scarguard.training.os-manifest="/app/os-packages.manifest"

ENV PYTHONPATH=/app/src:/app/shared
ENV PYTHONUNBUFFERED=1
ENV YOLO_CONFIG_DIR=/tmp/ultralytics
ENV VERSION=${VERSION}
ENV GIT_COMMIT=${GIT_COMMIT}
ENV BUILD_DATE=${BUILD_DATE}
ENV SCARGUARD_IMAGE=scarguard-trainer
ENV SCARGUARD_BASE_IMAGE=dustynv/l4t-pytorch:r36.4.0@sha256:a05c85def9139c21014546451d3baab44052d7cabe854d937f163390bfd5201b
ENV SCARGUARD_TORCH_VERSION=2.4.0
ENV SCARGUARD_CUDA_VERSION=12.6

RUN useradd -r -m -d /home/scarguard -s /usr/sbin/nologin -G video scarguard && \
    mkdir -p /data /config /models /tmp/ultralytics /data/training_uploads /data/training_workspace && \
    chown scarguard:scarguard /data /config /models /tmp/ultralytics

COPY services/trainer/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python3", "src/main.py"]
