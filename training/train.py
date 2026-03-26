#!/usr/bin/env python3
"""ScarGuard — Fine-tune a YOLO model on an exported dataset.

Usage:
    python train.py --data /path/to/dataset/data.yaml \
        --base-model yolov8n.pt \
        --output /models/heron_v1.pt

All hyperparameters have sensible defaults and can be overridden via CLI.
The script validates the dataset structure before training starts.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Fine-tune a YOLO model on a ScarGuard exported dataset.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--data", type=str, required=True,
        help="Path to YOLO data.yaml (from ScarGuard export)",
    )
    p.add_argument(
        "--base-model", type=str, default="yolov8n.pt",
        help="Pretrained YOLO checkpoint to fine-tune from",
    )
    p.add_argument(
        "--output", type=str, default="best.pt",
        help="Output path for the trained model weights",
    )
    p.add_argument("--epochs", type=int, default=100, help="Training epochs")
    p.add_argument("--imgsz", type=int, default=640, help="Input image size")
    p.add_argument("--batch", type=int, default=16, help="Batch size")
    p.add_argument(
        "--patience", type=int, default=20,
        help="Early stopping patience (epochs without improvement)",
    )
    p.add_argument(
        "--device", type=str, default="0",
        help="Device to train on (0 for GPU, cpu for CPU)",
    )
    p.add_argument(
        "--force", action="store_true",
        help="Proceed even if a class has fewer than 500 images",
    )
    return p.parse_args()


def _validate_dataset(data_yaml: Path) -> dict:
    """Validate that the dataset structure is correct.

    Returns the parsed YAML content.
    """
    import yaml

    if not data_yaml.exists():
        print(f"ERROR: data.yaml not found at {data_yaml}", file=sys.stderr)
        sys.exit(1)

    with open(data_yaml) as f:
        data = yaml.safe_load(f)

    if not data:
        print("ERROR: data.yaml is empty", file=sys.stderr)
        sys.exit(1)

    # Resolve paths relative to data.yaml location
    base_dir = data_yaml.parent
    train_images = base_dir / (data.get("train", "images/train"))

    if not train_images.exists():
        print(f"ERROR: Training images directory not found: {train_images}", file=sys.stderr)
        sys.exit(1)

    # Count images per class from label files
    labels_dir = base_dir / train_images.name.replace("images", "labels")
    if not labels_dir.exists():
        # Try the standard structure
        labels_dir = base_dir / "labels" / "train"

    class_names: list[str] = data.get("names", [])
    nc: int = data.get("nc", len(class_names))

    image_files = list(train_images.glob("*.jpg")) + list(train_images.glob("*.png"))
    print(f"Dataset: {len(image_files)} images, {nc} classes: {class_names}")

    if labels_dir.exists():
        class_counts: dict[int, int] = {}
        for lf in labels_dir.glob("*.txt"):
            for line in lf.read_text().strip().splitlines():
                parts = line.strip().split()
                if parts:
                    cls_id = int(parts[0])
                    class_counts[cls_id] = class_counts.get(cls_id, 0) + 1

        for idx, name in enumerate(class_names):
            count = class_counts.get(idx, 0)
            status = "OK" if count >= 500 else "LOW"
            print(f"  [{status}] {name}: {count} annotations")

    return data


def _count_per_class(data_yaml: Path, class_names: list[str]) -> dict[int, int]:
    """Count annotations per class from label files."""
    base_dir = data_yaml.parent
    labels_dir = base_dir / "labels" / "train"
    class_counts: dict[int, int] = {}
    if labels_dir.exists():
        for lf in labels_dir.glob("*.txt"):
            for line in lf.read_text().strip().splitlines():
                parts = line.strip().split()
                if parts:
                    cls_id = int(parts[0])
                    class_counts[cls_id] = class_counts.get(cls_id, 0) + 1
    return class_counts


def main() -> None:
    args = _parse_args()
    data_yaml = Path(args.data).resolve()

    print("=" * 60)
    print("ScarGuard Model Training")
    print("=" * 60)

    data = _validate_dataset(data_yaml)
    class_names: list[str] = data.get("names", [])

    # Check minimum image count per class
    class_counts = _count_per_class(data_yaml, class_names)
    low_classes = [
        class_names[idx]
        for idx in range(len(class_names))
        if class_counts.get(idx, 0) < 500
    ]
    if low_classes and not args.force:
        print(
            f"\nWARNING: The following classes have fewer than 500 annotations: "
            f"{', '.join(low_classes)}",
            file=sys.stderr,
        )
        print("Use --force to proceed anyway.", file=sys.stderr)
        sys.exit(1)

    # Import ultralytics (heavy import, do it after validation)
    try:
        from ultralytics import YOLO
    except ImportError:
        print(
            "ERROR: ultralytics not installed. Run: pip install ultralytics",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"\nBase model: {args.base_model}")
    print(f"Epochs: {args.epochs}, Image size: {args.imgsz}, Batch: {args.batch}")
    print(f"Patience: {args.patience}, Device: {args.device}")
    print()

    # Load base model and train
    model = YOLO(args.base_model)
    results = model.train(
        data=str(data_yaml),
        epochs=args.epochs,
        imgsz=args.imgsz,
        batch=args.batch,
        patience=args.patience,
        device=args.device,
        verbose=True,
    )

    # Copy best weights to output path
    output_path = Path(args.output).resolve()
    best_weights = Path(model.trainer.best)  # type: ignore[union-attr]
    if best_weights.exists():
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(best_weights), str(output_path))
        print(f"\nBest weights saved to: {output_path}")
    else:
        print("\nWARNING: Best weights file not found", file=sys.stderr)

    # Print summary metrics
    print("\n" + "=" * 60)
    print("Training Complete — Summary")
    print("=" * 60)

    if hasattr(results, "results_dict"):
        rd = results.results_dict
        print(f"  mAP@0.5:      {rd.get('metrics/mAP50(B)', 'N/A')}")
        print(f"  mAP@0.5:0.95: {rd.get('metrics/mAP50-95(B)', 'N/A')}")
        print(f"  Precision:     {rd.get('metrics/precision(B)', 'N/A')}")
        print(f"  Recall:        {rd.get('metrics/recall(B)', 'N/A')}")

    print(f"\nModel saved to: {output_path}")
    print("To use this model, copy it to your models directory and update")
    print("detection.model_path in scarguard.yml (or select it in the web UI).")


if __name__ == "__main__":
    main()
