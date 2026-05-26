#!/usr/bin/env python3
"""Prepare a merged YOLO dataset for ScarGuard model training.

Pulls labeled data from the Orin via SSH + docker cp, downloads
Roboflow Universe datasets and Open Images V6 annotations, remaps
all class indices to a unified scheme, and writes a single
YOLO-format dataset with a train/val split.

Prerequisites:
    pip install roboflow pyyaml requests

Usage:
    python prepare_dataset.py \
        --orin-host scott@orin \
        --roboflow-key YOUR_KEY \
        --output ./merged_dataset

    # Then train:
    python train.py --data ./merged_dataset/data.yaml --output pond_v1.pt
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import NamedTuple

UNIFIED_CLASSES = ["duck", "heron", "raccoon"]

CLASS_MAP: dict[str, str] = {
    "great_blue_heron": "heron",
    "green_heron": "heron",
    "blue_heron": "heron",
    "heron": "heron",
    "duck": "duck",
    "mallard": "duck",
    "raccoon": "raccoon",
    "raccon": "raccoon",
}

ROBOFLOW_DATASETS = [
    ("louis-berndroth2-gmail-com", "heron-detection"),
    ("harbin-institute-of-technology-hpsg8", "raccon-3osqx"),
]

OID_CLASSES: dict[str, str] = {
    "/m/09ddx": "duck",
    "/m/0dq75": "raccoon",
}

OID_ANNOTATIONS = {
    "train": "https://storage.googleapis.com/openimages/v6/oidv6-train-annotations-bbox.csv",
    "validation": "https://storage.googleapis.com/openimages/2018_04/validation/validation-annotations-bbox.csv",
    "test": "https://storage.googleapis.com/openimages/2018_04/test/test-annotations-bbox.csv",
}

OID_IMAGE_BASE = "https://open-images-dataset.s3.amazonaws.com"


class Sample(NamedTuple):
    image: Path
    label_lines: list[str]
    source: str


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Prepare a merged YOLO dataset for ScarGuard training.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--orin-host", default="scott@orin",
        help="SSH target for pulling ScarGuard data",
    )
    p.add_argument(
        "--orin-container", default="",
        help="Web container name on Orin (auto-detected if empty)",
    )
    p.add_argument(
        "--scarguard-zip", default="",
        help="Pre-downloaded ScarGuard export zip (skips SSH pull)",
    )
    p.add_argument(
        "--roboflow-key", default="",
        help="Roboflow API key for downloading Universe datasets",
    )
    p.add_argument(
        "--output", default="./merged_dataset",
        help="Output directory for the merged dataset",
    )
    p.add_argument(
        "--val-split", type=float, default=0.15,
        help="Fraction of images held out for validation",
    )
    p.add_argument("--seed", type=int, default=42, help="Random seed for split")
    p.add_argument(
        "--skip-orin", action="store_true",
        help="Skip pulling from Orin",
    )
    p.add_argument(
        "--skip-roboflow", action="store_true",
        help="Skip Roboflow downloads",
    )
    p.add_argument(
        "--skip-oid", action="store_true",
        help="Skip Open Images downloads",
    )
    p.add_argument(
        "--max-oid-per-class", type=int, default=1500,
        help="Cap Open Images images per class to keep dataset balanced",
    )
    p.add_argument(
        "--oid-workers", type=int, default=16,
        help="Parallel download threads for Open Images",
    )
    p.add_argument(
        "--local-db", default="",
        help="Path to local scarguard.db (replaces SSH pull when running on Orin)",
    )
    p.add_argument(
        "--local-snapshots", default="",
        help="Snapshot directory for local DB mode",
    )
    p.add_argument(
        "--training-uploads-db", default="",
        help="Path to DB with training_events table (video upload annotations)",
    )
    p.add_argument(
        "--training-uploads-frames", default="",
        help="Root frames directory for training uploads",
    )
    p.add_argument(
        "--skip-training-uploads", action="store_true",
        help="Skip training uploads source",
    )
    p.add_argument(
        "--background-sample-interval", type=int, default=10,
        help="Export every Nth frame from background uploads as negative sample",
    )
    return p.parse_args()


# ── Class mapping ──────────────────────────────────────────────────────────


def _resolve_class(name: str) -> str | None:
    """Map any source class name to a unified target class."""
    if name in CLASS_MAP:
        return CLASS_MAP[name]
    lower = name.lower().strip().replace(" ", "_")
    for src, dst in CLASS_MAP.items():
        if src.lower() == lower:
            return dst
    for target in UNIFIED_CLASSES:
        if target in lower:
            return target
    return None


# ── SSH helpers ────────────────────────────────────────────────────────────


def _ssh(host: str, cmd: str, *, timeout: int = 120) -> str:
    r = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, cmd],
        capture_output=True, text=True, timeout=timeout,
    )
    if r.returncode != 0:
        raise RuntimeError(f"SSH failed: {cmd}\n  stderr: {r.stderr.strip()}")
    return r.stdout.strip()


def _scp(src: str, dst: str, *, timeout: int = 300) -> None:
    subprocess.run(["scp", "-q", src, dst], check=True, timeout=timeout)


# ── Orin pull ──────────────────────────────────────────────────────────────


def _pull_orin_ssh(host: str, container: str, work_dir: Path) -> list[Sample]:
    """Pull ScarGuard training data from the Orin via SSH + docker cp."""
    print(f"\n{'='*60}")
    print(f"Pulling ScarGuard data from {host}")
    print("=" * 60)

    if not container:
        container = _ssh(
            host, "docker ps --format '{{.Names}}' | grep -i web | head -1",
        )
    if not container:
        print("  ERROR: Could not find web container on Orin", file=sys.stderr)
        return []
    print(f"  Container: {container}")

    remote_tmp = "/tmp/_sg_train_export"
    _ssh(host, f"rm -rf {remote_tmp} && mkdir -p {remote_tmp}")

    _ssh(host, f"docker cp {container}:/data/scarguard.db {remote_tmp}/db.sqlite")
    local_db = work_dir / "scarguard.db"
    _scp(f"{host}:{remote_tmp}/db.sqlite", str(local_db))
    print(f"  Database: {local_db.stat().st_size / 1_048_576:.1f} MB")

    conn = sqlite3.connect(str(local_db))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, class_name, snapshot_path, bbox, frame_size,
               feedback, corrected_class
        FROM detection_events
        WHERE camera_name != '_system'
          AND feedback IN ('correct', 'wrong_class', 'false_positive')
          AND snapshot_path IS NOT NULL
          AND (feedback = 'false_positive' OR bbox IS NOT NULL)
        ORDER BY id
    """).fetchall()
    conn.close()

    if not rows:
        print("  No exportable events found")
        _ssh(host, f"rm -rf {remote_tmp}")
        return []
    print(f"  {len(rows)} exportable events")

    snap_names = sorted({Path(r["snapshot_path"]).name for r in rows})
    print(f"  Transferring {len(snap_names)} snapshots...")

    listing = "\n".join(snap_names)
    _ssh(host, f"cat > {remote_tmp}/needed.txt << 'SNAPEOF'\n{listing}\nSNAPEOF")
    _ssh(
        host,
        f"docker cp {container}:/data/snapshots {remote_tmp}/snapshots",
        timeout=300,
    )

    snap_dir = work_dir / "snapshots"
    snap_dir.mkdir()
    result = subprocess.run(
        [
            "ssh", "-o", "BatchMode=yes", host,
            f"cd {remote_tmp}/snapshots && tar cf - -T {remote_tmp}/needed.txt 2>/dev/null",
        ],
        capture_output=True, timeout=600,
    )
    if result.stdout:
        tar_path = work_dir / "snapshots.tar"
        tar_path.write_bytes(result.stdout)
        subprocess.run(
            ["tar", "xf", str(tar_path), "-C", str(snap_dir)],
            check=True, capture_output=True,
        )
        print(f"  Snapshots: {tar_path.stat().st_size / 1_048_576:.1f} MB")
    else:
        print("  WARNING: tar produced no output — snapshot transfer may have failed")

    _ssh(host, f"rm -rf {remote_tmp}")
    return _rows_to_samples(rows, snap_dir)


def _pull_orin_zip(zip_path: Path, work_dir: Path) -> list[Sample]:
    """Build samples from a pre-downloaded ScarGuard export zip."""
    import zipfile

    import yaml

    print(f"\n{'='*60}")
    print(f"Loading ScarGuard export from {zip_path}")
    print("=" * 60)

    dest = work_dir / "sg_zip"
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest)

    ds_dir = dest / "dataset" if (dest / "dataset").exists() else dest
    data_yaml = ds_dir / "data.yaml"
    if not data_yaml.exists():
        print("  ERROR: No data.yaml in zip", file=sys.stderr)
        return []

    with open(data_yaml) as f:
        dy = yaml.safe_load(f)
    src_classes: list[str] = dy.get("names", [])
    if isinstance(src_classes, dict):
        src_classes = [src_classes[k] for k in sorted(src_classes.keys())]
    print(f"  Source classes: {src_classes}")

    remap = _build_remap(src_classes)
    return _dir_to_samples(ds_dir, remap, source="scarguard")


def _rows_to_samples(rows: list[sqlite3.Row], snap_dir: Path) -> list[Sample]:
    """Convert database rows + snapshot files into Sample objects."""
    samples: list[Sample] = []
    skipped = 0

    for r in rows:
        row = dict(r)
        snap_name = Path(row["snapshot_path"]).name
        img = snap_dir / snap_name
        if not img.exists():
            skipped += 1
            continue

        if row["feedback"] == "false_positive":
            samples.append(Sample(image=img, label_lines=[], source="scarguard"))
            continue

        eff_class = (
            row["corrected_class"]
            if row["feedback"] == "wrong_class" and row.get("corrected_class")
            else row["class_name"]
        )
        target = _resolve_class(eff_class)
        if target is None:
            print(f"    WARNING: Unmapped class '{eff_class}' in event {row['id']}")
            skipped += 1
            continue

        cls_idx = UNIFIED_CLASSES.index(target)
        bbox = json.loads(row["bbox"]) if isinstance(row["bbox"], str) else row["bbox"]
        fsize = (
            json.loads(row["frame_size"])
            if isinstance(row["frame_size"], str)
            else row["frame_size"]
        )
        x1, y1, x2, y2 = bbox
        fw, fh = fsize
        line = (
            f"{cls_idx} "
            f"{((x1 + x2) / 2) / fw:.6f} "
            f"{((y1 + y2) / 2) / fh:.6f} "
            f"{(x2 - x1) / fw:.6f} "
            f"{(y2 - y1) / fh:.6f}"
        )
        samples.append(Sample(image=img, label_lines=[line], source="scarguard"))

    print(f"  {len(samples)} samples ({skipped} skipped)")
    return samples


# ── Local DB pull (replaces SSH when running on-Orin) ──────────────────────


_ORIN_LABELED_QUERY = """
    SELECT id, class_name, snapshot_path, bbox, frame_size,
           feedback, corrected_class
    FROM detection_events
    WHERE camera_name != '_system'
      AND feedback IN ('correct', 'wrong_class', 'false_positive')
      AND snapshot_path IS NOT NULL
      AND (feedback = 'false_positive' OR bbox IS NOT NULL)
    ORDER BY id
"""


def _pull_orin_local(db_path: str, snapshots_dir: str) -> list[Sample]:
    """Read detection_events directly from a local SQLite DB."""
    print(f"\n── Local DB: {db_path} ──")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(_ORIN_LABELED_QUERY).fetchall()
    finally:
        conn.close()
    print(f"  {len(rows)} labeled events")
    return _rows_to_samples(rows, Path(snapshots_dir))


# ── Training uploads pull (video upload annotations) ──────────────────────


def pull_training_uploads(
    db_path: str,
    frames_dir: str,
    background_sample_interval: int = 10,
) -> list[Sample]:
    """Fourth source: approved annotations from video uploads."""
    print(f"\n── Training uploads: {db_path} ──")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        events = conn.execute("""
            SELECT te.id, te.upload_id, te.frame_idx, te.bbox,
                   te.predicted_class, te.confidence, te.review_state,
                   te.corrected_class, tu.target_class_hint
            FROM training_events te
            JOIN training_uploads tu ON te.upload_id = tu.id
            WHERE te.review_state IN ('approved', 'corrected')
            ORDER BY te.upload_id, te.frame_idx
        """).fetchall()
        bg_uploads = conn.execute("""
            SELECT id, frame_count FROM training_uploads
            WHERE target_class_hint = 'background'
              AND status = 'processed'
        """).fetchall()
    finally:
        conn.close()

    samples: list[Sample] = []
    skipped = 0

    # Positive samples: group by (upload_id, frame_idx) for multi-detection labels
    from collections import defaultdict
    frame_labels: dict[tuple[str, int], list[str]] = defaultdict(list)
    frame_paths: dict[tuple[str, int], Path] = {}

    for r in events:
        row = dict(r)
        cls = row["corrected_class"] if row["review_state"] == "corrected" and row.get("corrected_class") else row["predicted_class"]
        target = _resolve_class(cls)
        if target is None:
            print(f"    WARNING: Unmapped class '{cls}' in training_event {row['id']}")
            skipped += 1
            continue

        cls_idx = UNIFIED_CLASSES.index(target)
        bbox = json.loads(row["bbox"]) if isinstance(row["bbox"], str) else row["bbox"]
        line = f"{cls_idx} {bbox[0]:.6f} {bbox[1]:.6f} {bbox[2]:.6f} {bbox[3]:.6f}"
        key = (row["upload_id"], row["frame_idx"])
        frame_labels[key].append(line)
        frame_paths[key] = Path(frames_dir) / row["upload_id"] / "frames" / f"{row['frame_idx']:06d}.jpg"

    for key, lines in frame_labels.items():
        img = frame_paths[key]
        if img.exists():
            samples.append(Sample(image=img, label_lines=lines, source="training_uploads"))
        else:
            skipped += 1

    # Background samples: every Nth frame from background uploads
    bg_count = 0
    for upload in bg_uploads:
        upload_frames = Path(frames_dir) / upload["id"] / "frames"
        if not upload_frames.exists():
            continue
        fc = upload["frame_count"] or 0
        for i in range(0, fc, max(1, background_sample_interval)):
            fp = upload_frames / f"{i:06d}.jpg"
            if fp.exists():
                samples.append(Sample(image=fp, label_lines=[], source="training_uploads"))
                bg_count += 1

    print(f"  {len(samples)} samples ({skipped} skipped, {bg_count} background)")
    return samples


# ── Roboflow pull ──────────────────────────────────────────────────────────


def pull_roboflow(api_key: str, work_dir: Path) -> list[Sample]:
    """Download Roboflow Universe datasets and return remapped samples."""
    try:
        from roboflow import Roboflow
    except ImportError:
        print("ERROR: pip install roboflow", file=sys.stderr)
        sys.exit(1)

    import yaml

    print(f"\n{'='*60}")
    print("Downloading Roboflow datasets")
    print("=" * 60)

    rf = Roboflow(api_key=api_key)
    all_samples: list[Sample] = []

    for ws_name, proj_name in ROBOFLOW_DATASETS:
        tag = f"{ws_name}/{proj_name}"
        print(f"\n  [{tag}]")
        try:
            project = rf.project(tag)
            version = _latest_version(project)
            if version is None:
                print("    No versions available, skipping")
                continue

            dl_dir = work_dir / "roboflow" / proj_name
            version.download("yolov8", location=str(dl_dir))

            data_yaml = dl_dir / "data.yaml"
            if not data_yaml.exists():
                print("    WARNING: No data.yaml, skipping")
                continue

            with open(data_yaml) as f:
                dy = yaml.safe_load(f)
            src_classes: list[str] = dy.get("names", [])
            if isinstance(src_classes, dict):
                src_classes = [src_classes[k] for k in sorted(src_classes.keys())]
            print(f"    Classes: {src_classes}")

            remap = _build_remap(src_classes)
            samples = _dir_to_samples(dl_dir, remap, source=proj_name)
            all_samples.extend(samples)
            print(f"    {len(samples)} samples")

        except Exception as e:
            print(f"    ERROR: {e}")

    return all_samples


def _latest_version(project: object) -> object | None:
    """Return the latest version of a Roboflow project."""
    try:
        versions = project.versions()  # type: ignore[union-attr]
        return versions[0] if versions else None
    except Exception:
        pass
    for n in range(10, 0, -1):
        try:
            return project.version(n)  # type: ignore[union-attr]
        except Exception:
            continue
    return None


# ── Open Images pull ──────────────────────────────────────────────────────


def pull_open_images(
    work_dir: Path,
    max_per_class: int,
    workers: int,
) -> list[Sample]:
    """Download duck + raccoon images from Open Images V6."""
    import requests

    print(f"\n{'='*60}")
    print("Downloading Open Images (duck + raccoon)")
    print("=" * 60)

    # Collect annotations: {image_id: {split, class, bboxes[]}}
    image_data: dict[str, dict] = {}
    class_image_counts: dict[str, int] = {cls: 0 for cls in OID_CLASSES.values()}

    for split, url in OID_ANNOTATIONS.items():
        print(f"\n  Scanning {split} annotations...")
        try:
            resp = requests.get(url, stream=True, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            print(f"    ERROR downloading annotations: {e}")
            continue

        lines_iter = resp.iter_lines(decode_unicode=True)
        header_line = next(lines_iter)
        cols = header_line.split(",")
        img_col = cols.index("ImageID")
        label_col = cols.index("LabelName")
        xmin_col = cols.index("XMin")
        xmax_col = cols.index("XMax")
        ymin_col = cols.index("YMin")
        ymax_col = cols.index("YMax")

        row_count = 0
        for line in lines_iter:
            parts = line.split(",")
            label = parts[label_col]
            if label not in OID_CLASSES:
                row_count += 1
                continue

            target_cls = OID_CLASSES[label]
            img_id = parts[img_col]

            if img_id not in image_data:
                if class_image_counts[target_cls] >= max_per_class:
                    row_count += 1
                    continue
                class_image_counts[target_cls] += 1
                image_data[img_id] = {
                    "split": split,
                    "bboxes": [],
                }

            image_data[img_id]["bboxes"].append({
                "class": target_cls,
                "xmin": float(parts[xmin_col]),
                "xmax": float(parts[xmax_col]),
                "ymin": float(parts[ymin_col]),
                "ymax": float(parts[ymax_col]),
            })
            row_count += 1
            if row_count % 5_000_000 == 0:
                print(
                    f"    ...{row_count / 1e6:.0f}M rows  "
                    f"duck={class_image_counts['duck']}  "
                    f"raccoon={class_image_counts['raccoon']}",
                )

            # Stop scanning if we have enough of everything
            if all(c >= max_per_class for c in class_image_counts.values()):
                break

        for cls, cnt in sorted(class_image_counts.items()):
            print(f"    {cls}: {cnt} images so far")

    if not image_data:
        print("  No images found")
        return []

    # Download images in parallel
    img_dir = work_dir / "oid_images"
    img_dir.mkdir(parents=True, exist_ok=True)

    image_ids = list(image_data.keys())
    print(f"\n  Downloading {len(image_ids)} images ({workers} threads)...")

    def _download_one(img_id: str) -> bool:
        split = image_data[img_id]["split"]
        url = f"{OID_IMAGE_BASE}/{split}/{img_id}.jpg"
        dest = img_dir / f"{img_id}.jpg"
        try:
            r = requests.get(url, timeout=30)
            if r.status_code == 200 and len(r.content) > 1000:
                dest.write_bytes(r.content)
                return True
        except Exception:
            pass
        return False

    downloaded = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_download_one, iid): iid for iid in image_ids}
        for i, future in enumerate(as_completed(futures), 1):
            if future.result():
                downloaded += 1
            else:
                failed += 1
            if i % 200 == 0:
                print(f"    ...{i}/{len(image_ids)} ({downloaded} ok, {failed} failed)")

    print(f"  Downloaded {downloaded}/{len(image_ids)} images ({failed} failed)")

    # Build samples with YOLO-format labels
    samples: list[Sample] = []
    for img_id, data in image_data.items():
        img_path = img_dir / f"{img_id}.jpg"
        if not img_path.exists():
            continue

        lines: list[str] = []
        for bbox in data["bboxes"]:
            cls_idx = UNIFIED_CLASSES.index(bbox["class"])
            xmin, xmax = bbox["xmin"], bbox["xmax"]
            ymin, ymax = bbox["ymin"], bbox["ymax"]
            x_center = (xmin + xmax) / 2
            y_center = (ymin + ymax) / 2
            width = xmax - xmin
            height = ymax - ymin
            lines.append(
                f"{cls_idx} {x_center:.6f} {y_center:.6f} {width:.6f} {height:.6f}"
            )
        samples.append(Sample(image=img_path, label_lines=lines, source="open-images"))

    print(f"  {len(samples)} samples ready")
    return samples


# ── Dataset I/O helpers ───────────────────────────────────────────────────


def _build_remap(src_classes: list[str]) -> dict[int, int]:
    """Build source-index -> unified-index mapping."""
    remap: dict[int, int] = {}
    for src_idx, name in enumerate(src_classes):
        target = _resolve_class(name)
        if target is not None:
            remap[src_idx] = UNIFIED_CLASSES.index(target)
        else:
            print(f"    WARNING: Unmapped class '{name}' (annotations dropped)")
    return remap


def _dir_to_samples(
    ds_dir: Path,
    remap: dict[int, int],
    source: str,
) -> list[Sample]:
    """Read a YOLO dataset directory and return remapped samples.

    Handles both layouts:
      - Standard: images/train, labels/train
      - Roboflow: train/images, train/labels
    """
    samples: list[Sample] = []

    for split in ("train", "valid", "val", "test"):
        for img_dir, lbl_dir in [
            (ds_dir / split / "images", ds_dir / split / "labels"),
            (ds_dir / "images" / split, ds_dir / "labels" / split),
        ]:
            if not img_dir.exists():
                continue
            for img in sorted(img_dir.iterdir()):
                if img.suffix.lower() not in (".jpg", ".jpeg", ".png"):
                    continue
                lbl = lbl_dir / f"{img.stem}.txt"
                lines: list[str] = []
                if lbl.exists():
                    for raw in lbl.read_text().strip().splitlines():
                        parts = raw.strip().split()
                        if not parts:
                            continue
                        src_idx = int(parts[0])
                        if src_idx in remap:
                            parts[0] = str(remap[src_idx])
                            lines.append(" ".join(parts))
                samples.append(Sample(image=img, label_lines=lines, source=source))
            break

    return samples


# ── Merge + split ─────────────────────────────────────────────────────────


def merge_and_split(
    samples: list[Sample],
    output_dir: Path,
    val_split: float,
    seed: int,
) -> None:
    """Write a unified YOLO dataset with a randomised train/val split."""
    print(f"\n{'='*60}")
    print(f"Merging {len(samples)} samples")
    print("=" * 60)

    if output_dir.exists():
        shutil.rmtree(output_dir)
    for sub in ("images/train", "images/val", "labels/train", "labels/val"):
        (output_dir / sub).mkdir(parents=True)

    rng = random.Random(seed)
    indices = list(range(len(samples)))
    rng.shuffle(indices)
    n_val = max(1, int(len(samples) * val_split))
    val_set = set(indices[:n_val])

    source_counts: dict[str, int] = {}
    class_counts: dict[str, int] = {}
    n_background = 0
    seen_stems: set[str] = set()

    for i, sample in enumerate(samples):
        split = "val" if i in val_set else "train"

        stem = f"{sample.source}_{sample.image.stem}"
        if stem in seen_stems:
            n = 2
            while f"{stem}_{n}" in seen_stems:
                n += 1
            stem = f"{stem}_{n}"
        seen_stems.add(stem)

        ext = sample.image.suffix
        shutil.copy2(
            str(sample.image),
            str(output_dir / "images" / split / f"{stem}{ext}"),
        )

        content = "\n".join(sample.label_lines) + "\n" if sample.label_lines else ""
        (output_dir / "labels" / split / f"{stem}.txt").write_text(content)

        source_counts[sample.source] = source_counts.get(sample.source, 0) + 1
        if sample.label_lines:
            for line in sample.label_lines:
                cls_idx = int(line.split()[0])
                cls_name = UNIFIED_CLASSES[cls_idx]
                class_counts[cls_name] = class_counts.get(cls_name, 0) + 1
        else:
            n_background += 1

    (output_dir / "data.yaml").write_text(
        f"# ScarGuard merged training dataset\n"
        f"path: .\n"
        f"train: images/train\n"
        f"val: images/val\n"
        f"\n"
        f"nc: {len(UNIFIED_CLASSES)}\n"
        f"names: {UNIFIED_CLASSES}\n"
    )

    n_train = len(samples) - n_val
    print(f"\n  Train: {n_train}   Val: {n_val}")
    print("\n  By source:")
    for src, cnt in sorted(source_counts.items()):
        print(f"    {src}: {cnt}")
    print("\n  By class (annotation count):")
    for cls in UNIFIED_CLASSES:
        cnt = class_counts.get(cls, 0)
        status = "OK" if cnt >= 500 else "LOW"
        print(f"    [{status}] {cls}: {cnt}")
    if n_background:
        print(f"    [bg]  background: {n_background}")
    print(f"\n  Output: {output_dir / 'data.yaml'}")


# ── Main ──────────────────────────────────────────────────────────────────


def main() -> None:
    args = _parse_args()
    output_dir = Path(args.output).resolve()

    if not args.skip_roboflow and not args.roboflow_key:
        print("ERROR: --roboflow-key is required (or use --skip-roboflow)", file=sys.stderr)
        sys.exit(1)

    with tempfile.TemporaryDirectory(prefix="sg_train_") as tmp:
        work_dir = Path(tmp)
        samples: list[Sample] = []

        if not args.skip_orin:
            if args.local_db:
                samples.extend(_pull_orin_local(args.local_db, args.local_snapshots or "/data/snapshots"))
            elif args.scarguard_zip:
                samples.extend(_pull_orin_zip(Path(args.scarguard_zip), work_dir))
            else:
                samples.extend(
                    _pull_orin_ssh(args.orin_host, args.orin_container, work_dir),
                )

        if not args.skip_roboflow:
            samples.extend(pull_roboflow(args.roboflow_key, work_dir))

        if not args.skip_oid:
            samples.extend(
                pull_open_images(work_dir, args.max_oid_per_class, args.oid_workers),
            )

        if not args.skip_training_uploads and args.training_uploads_db:
            samples.extend(
                pull_training_uploads(
                    args.training_uploads_db,
                    args.training_uploads_frames or "/data/training_uploads",
                    args.background_sample_interval,
                ),
            )

        if not samples:
            print("\nERROR: No samples collected from any source", file=sys.stderr)
            sys.exit(1)

        merge_and_split(samples, output_dir, args.val_split, args.seed)

    print("\nDone! Next step:")
    print(f"  python train.py --data {output_dir}/data.yaml --output pond_v1.pt")


if __name__ == "__main__":
    main()
