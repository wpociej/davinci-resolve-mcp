# Auto Magic Mask Seed

Plugin for DaVinci Resolve MCP — auto-seeds person detection on every clip in
the active timeline using mediapipe BlazeFace face detection + UI automation.

Resolve's `CreateMagicMask` API requires a seed point clicked in the viewer
before it can track. This plugin does the click for you for every clip.

**How it works:** for each clip the plugin reads the corresponding frame
*directly from the source file* (cv2.VideoCapture) and runs mediapipe BlazeFace
face detection on the full source resolution — no race condition with Resolve's
viewer, no broken screenshots if the 4K HEVC decoder lags. The detected
face position is then mapped to absolute screen coordinates inside the
calibrated viewer rectangle, and pyautogui clicks there before the
`CreateMagicMask` API call.

## Requirements

- Windows (uses pywinauto; macOS/Linux can be added — file an issue)
- Python 3.10–3.12 from this repo's venv
- DaVinci Resolve Studio with external scripting enabled
- The MCP server's venv has all deps installed (`pywinauto pyautogui mss opencv-python mediapipe`)

## Usage

**Pre-flight (manual, in Resolve):**

1. Open the Color page (Shift+6 or click "Color" tab at the bottom)
2. Open the Magic Mask palette (lower-left toolbar)
3. Set tracker mode to **People & Object**, subject **Person**

**Then run:**

```bash
# From the davinci-resolve-mcp directory:
venv\Scripts\python.exe tools\auto_magic_mask\auto_magic_mask.py [options]
```

### First-time calibration (mandatory before real-mode runs)

The plugin needs to know **where on screen the visible clip image is drawn
inside the Resolve viewer**, so its synthetic clicks land in the right
place. Calibrate once before the first real run:

```bash
python auto_magic_mask.py --calibrate
```

The calibration is interactive: it asks you to

1. Move the mouse to the **top-left** corner of the visible clip image
   area in the viewer (where the picture itself starts — NOT the viewer
   panel borders or letterbox bars), then press Enter.
2. Move the mouse to the **bottom-right** corner of the visible image
   area, then press Enter.
3. Confirm by reviewing the saved sanity-screenshot.

Saves to `~/.davinci_auto_magic_mask.json` and reuses it on later runs.

**Re-calibrate** if you change Resolve's window size, panel layout, or
monitor configuration. Stale calibration = clicks landing on UI chrome.

The MCP wrapper (`auto_seed_magic_mask` tool) refuses to run real-mode
without calibration — it returns an error pointing here. Dry-run mode
works without calibration (no clicks).

### Process all clips (the real run)

```bash
python auto_magic_mask.py
```

### Process specific clips only

```bash
python auto_magic_mask.py --clips 1,3,5,9
```

### Dry run (no clicking, no API trigger)

```bash
python auto_magic_mask.py --dry-run --debug
```

The `--debug` flag saves annotated screenshots to `auto_mm_debug/` so you can
verify face detection visually.

## Options

| flag | default | what |
|---|---|---|
| `--calibrate` | — | interactive viewer-rect setup |
| `--clips` | `all` | comma-separated indices or 'all' |
| `--dry-run` | off | don't click/trigger |
| `--debug` | off | save annotated screenshots |
| `--debug-dir` | `auto_mm_debug` | where to save debug images |
| `--seed-delay` | 1.0 | seconds to wait after click |
| `--playhead-delay` | 0.6 | seconds to wait after seeking |

## Limitations

- **Magic Mask palette must be open beforehand** — Resolve API has no method
  to open it programmatically.
- **Face detection only** — BlazeFace is a face detector, so for non-human
  subjects (animals, objects) it won't help. Future: integrate SAM / Claude
  vision for arbitrary subjects.
- **Profile shots and partial occlusion** may fail face detection — the
  clip is logged as skipped, not failed. Re-run on those clips manually.
- **Source-file dependence** — the plugin reads frames from the original
  media path (item.GetMediaPoolItem().GetClipProperty("File Path")). If
  media is offline or relinked elsewhere it falls back to "skip".
- **Aspect ratio assumption** — source is assumed to fit the viewer with
  letterbox/pillarbox preserving aspect ratio. If you've heavily transformed
  the clip (custom Pan/Tilt/Zoom in Inspector), the click position can drift
  off the actual face. For typical full-frame clips this works fine.
- **Window must be focused** during the run. Don't switch to other apps.
- **Multi-monitor**: pywinauto returns absolute screen coords (including
  negative for left-of-primary monitors); pyautogui handles negative coords
  fine on Windows 10+.
