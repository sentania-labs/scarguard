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

### How feedback maps to training data

| Feedback | What lands in the export |
|---|---|
| **Correct** | Image + bbox label using the model's predicted class |
| **Wrong Class** + corrected label | Image + bbox label using the *corrected* class. The bbox is the model's *original* detection — see the gotcha below. |
| **False Positive** | Image + **empty** `.txt` label file. YOLO treats this as a background sample ("nothing of interest here") and learns what the *absence* of a target looks like in your specific environment. |

### Gotcha: corrected_class bbox

When you mark an event as **Wrong Class** and provide a corrected label
(e.g. model said "person", you typed "heron"), the **bbox stored on
that event is the model's original detection**, not a fresh box around
the actual heron. So the corrected label is only useful when the model
detected *near* the right place — i.e. it boxed something that overlaps
the real target.

For events where the model boxed the wrong thing entirely (heron in the
upper-right of the frame, model boxed a person in the foreground), the
corrected label points YOLO at the wrong pixels. Two ways to handle:

- **Skip those events** when labelling — leave them unlabelled rather
  than corrupting the training set with bad bboxes.
- **Wait for v1.15** — a "redraw bbox" UI affordance is planned for
  v1.15 (paired with the polygon zone editor — same canvas tooling) so
  you can drop a fresh box on the actual target when correcting class.

For the missing-detection case (heron in the frame, model didn't detect
at all), no event = no training sample. Mix in a third-party heron
dataset to fill that gap (see "Mixing third-party datasets" below).

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
    --output heron_v1.pt

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
        ├── 456.txt    # (empty file — false-positive event = background sample)
        └── ...
```

Empty `.txt` files are intentional: false-positive feedback exports
the snapshot with a zero-byte label file, which YOLO interprets as
"no targets in this image." These act as negative / background
samples during training.

## prepare_dataset.py — merged dataset builder

`prepare_dataset.py` automates the dataset merge: ScarGuard feedback
events (via SSH or local DB), training-upload annotations, Roboflow
Universe datasets, and Open Images downloads, remapped to one unified
class scheme with a train/val split. The trainer service runs this same
script for on-device `prepare_dataset` / `prepare_and_train` jobs.

```bash
python prepare_dataset.py \
    --orin-host scott@orin \
    --roboflow-key YOUR_KEY \
    --output ./merged_dataset

# Custom class list (order defines model class indices):
python prepare_dataset.py --classes duck,heron,raccoon ...
```

### Classes and distractors

The default class list is
`duck, heron, raccoon, person, dog, cat, plant`. The last four are
**distractor classes**: trained so the model has a correct bucket for
humans, pets, and vegetation instead of forcing them into the nearest
target silhouette (humans at the pond used to come back as "heron").
Distractor training data is pulled automatically from Open Images;
person/dog/cat/plant boxes you draw in the labeling UI count too.

- `--classes` takes a comma-separated **ordered** list — order defines
  the model's class indices, so keep it stable between runs you intend
  to compare.
- Labels that don't map to an active class are dropped with a warning:
  `--classes duck,heron,raccoon` reproduces the original 3-class
  behavior exactly.
- At runtime, distractors are filtered by `detection.target_classes` —
  see CONFIG_REFERENCE.md ("Distractor Classes & Runtime Behavior").

## Mixing third-party datasets

The exported zip is a standard YOLO dataset, so you can extend it
with public heron / duck / raccoon datasets to bulk up positive
samples while keeping your captured snapshots as the source of
*background* truth (what your pond and yard actually look like,
without targets).

Workflow:

1. Extract the ScarGuard export: `unzip scarguard_dataset.zip -d my_dataset`
2. Note its `dataset/data.yaml` — list of class names and their
   indices (e.g. `names: ['heron', 'person', 'raccoon']`).
3. For each third-party dataset, **renumber its label files** so the
   class indices match your `data.yaml`. If a third-party heron
   dataset uses class `0` for heron and your export uses class `0`
   for heron, no change needed. If indices differ, run `sed` over
   the `.txt` files: `sed -i 's/^0 /N /' labels/train/*.txt` where
   `N` is the right index in your data.yaml.
4. Copy the third-party `images/train/*` into your dataset's
   `images/train/` and the renumbered `labels/train/*.txt` into
   `labels/train/`. Filename collisions: rename third-party files
   with a prefix (e.g. `coco-`, `oid-`) to avoid clashing with
   ScarGuard's numeric event IDs.
5. Add any third-party class names to `data.yaml` `names:` if you
   added new ones. Update `nc:` to match `len(names)`.
6. Train against the merged `data.yaml`.

Result: model learns heron / duck / raccoon shape from the public
datasets *plus* learns what's-not-a-target in your specific yard
from your own false-positive captures.

## Jetson Orin Tips

- Use `--batch 4` or `--batch 8` to fit in 8GB GPU memory
- Use `--imgsz 640` (default) — larger sizes may OOM
- Training runs significantly slower than x86 GPUs; consider training on a workstation and copying the `.pt` file to the Jetson
- The trained `.pt` file can be converted to TensorRT `.engine` format for faster inference using `yolo export model=heron_v1.pt format=engine`

## Model Promotion

Training produces a `.pt` file. To use it:

1. Upload the `.pt` file via the web UI Models page, or copy it into the `scarguard-models` Docker volume
2. Update `detection.model_path` in `scarguard.yml` to point to the new model, or select it via the web UI Config page
3. The detector service will hot-reload the new model without a restart

**Model promotion is always manual** — the system never automatically deploys a trained model.
