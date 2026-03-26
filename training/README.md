# ScarGuard — Model Training

Fine-tune a YOLO model on detection data collected and labeled through ScarGuard.

## Prerequisites

- Python 3.11+
- `ultralytics` package: `pip install ultralytics`
- `pyyaml` package: `pip install pyyaml`
- NVIDIA GPU recommended (Jetson Orin or x86 with CUDA)

## Workflow

1. **Label detections** in the ScarGuard web UI — mark events as Correct, False Positive, or Wrong Class on the Events page
2. **Export dataset** from the Training Data admin page — downloads a YOLO-format zip
3. **Extract the zip** and run the training script
4. **Evaluate** the trained model against stored snapshots using the Model Evaluation page
5. **Promote** the model by copying the `.pt` file to your models directory and selecting it in config

## Usage

```bash
# Extract the exported dataset
unzip scarguard_dataset.zip -d my_dataset

# Train with defaults (100 epochs, 640px, batch 16)
python train.py --data my_dataset/dataset/data.yaml --output heron_v1.pt

# Custom hyperparameters
python train.py \
    --data my_dataset/dataset/data.yaml \
    --base-model yolov8s.pt \
    --epochs 200 \
    --imgsz 640 \
    --batch 8 \
    --patience 30 \
    --device 0 \
    --output /var/docker/scarguard/models/heron_v1.pt

# Force training even with < 500 images per class
python train.py --data my_dataset/dataset/data.yaml --force --output heron_v1.pt
```

## CLI Arguments

| Argument | Default | Description |
|---|---|---|
| `--data` | (required) | Path to YOLO `data.yaml` |
| `--base-model` | `yolov8n.pt` | Pretrained YOLO checkpoint to fine-tune |
| `--output` | `best.pt` | Output path for trained weights |
| `--epochs` | `100` | Training epochs |
| `--imgsz` | `640` | Input image size |
| `--batch` | `16` | Batch size (reduce for Jetson: try 4 or 8) |
| `--patience` | `20` | Early stopping patience |
| `--device` | `0` | Device (`0` for GPU, `cpu` for CPU) |
| `--force` | off | Proceed even if classes have < 500 images |

## Dataset Format

The exported zip contains a standard YOLO dataset structure:

```
dataset/
├── data.yaml          # Class names and paths
├── images/
│   └── train/
│       ├── 123.jpg    # Clean snapshot (no bbox overlay)
│       ├── 456.jpg
│       └── ...
└── labels/
    └── train/
        ├── 123.txt    # YOLO annotation: class_id x_center y_center width height
        ├── 456.txt
        └── ...
```

## Jetson Orin Tips

- Use `--batch 4` or `--batch 8` to fit in 8GB GPU memory
- Use `--imgsz 640` (default) — larger sizes may OOM
- Training runs significantly slower than x86 GPUs; consider training on a workstation and copying the `.pt` file to the Jetson
- The trained `.pt` file can be converted to TensorRT `.engine` format for faster inference using `yolo export model=heron_v1.pt format=engine`

## Model Promotion

Training produces a `.pt` file. To use it:

1. Copy the `.pt` file to your ScarGuard models directory (e.g., `/var/docker/scarguard/models/`)
2. Update `detection.model_path` in `scarguard.yml` to point to the new model, or select it via the web UI Config page
3. The detector service will hot-reload the new model without a restart

**Model promotion is always manual** — the system never automatically deploys a trained model.
