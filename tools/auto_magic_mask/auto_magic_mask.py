"""
auto_magic_mask.py — DaVinci Resolve Magic Mask auto-seeding via UI automation.

Workflow:
  1. Connect to Resolve via the scripting API (timeline must be open)
  2. Locate the Resolve main window with pywinauto
  3. For each clip:
     a. Move playhead to clip middle (Resolve API)
     b. Read the SAME frame directly from the source file (cv2.VideoCapture)
     c. Detect face on the source frame (full 4K, no viewer race conditions)
     d. Map face source-coords -> viewer screen coords + click (pyautogui)
     e. Trigger CreateMagicMask("BI") via Resolve API

Pre-flight (you must do this manually before running):
  • Open the Color page in Resolve
  • Open the Magic Mask palette (lower-left toolbar in Color page)
  • Make sure tracker mode is "People & Object"

Usage:
  python auto_magic_mask.py --calibrate          # interactive viewer-rect setup
  python auto_magic_mask.py --debug              # process all clips, save debug overlays
  python auto_magic_mask.py --clips 1,3,5        # specific clip indices (0-based)
  python auto_magic_mask.py --dry-run            # don't click/trigger, just report
"""

import argparse
import json
import os
import platform
import sys
import time
from pathlib import Path

# DPI awareness on Windows.
#
# On Windows, Python's launcher often pre-locks process DPI awareness via its
# manifest. SetProcessDpiAwarenessContext then silently no-ops, leaving us in
# SYSTEM_DPI_AWARE — which on multi-monitor with mixed DPIs makes
# pyautogui.position() and ImageGrab return coords in a virtualized space
# that doesn't match what the user visually clicks. The fix that survives a
# locked process awareness: SetThreadDpiAwarenessContext to
# PER_MONITOR_AWARE_V2 (-4) for the calling thread. We do this once at module
# load (covers our single-threaded use), and also via _per_monitor_aware()
# context manager around individual cursor/screenshot calls for safety.
import contextlib

@contextlib.contextmanager
def _per_monitor_aware():
    """Force PER_MONITOR_AWARE_V2 for the current thread within the with-block."""
    if platform.system() != "Windows":
        yield
        return
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.SetThreadDpiAwarenessContext.argtypes = [wintypes.HANDLE]
        user32.SetThreadDpiAwarenessContext.restype = wintypes.HANDLE
        prev = user32.SetThreadDpiAwarenessContext(-4)  # V2
    except Exception:
        yield
        return
    try:
        yield
    finally:
        try:
            user32.SetThreadDpiAwarenessContext(prev)
        except Exception:
            pass


if platform.system() == "Windows":
    try:
        import ctypes
        from ctypes import wintypes
        # Best-effort process-wide setup (often locked already).
        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
        # Override thread awareness — survives locked process awareness.
        try:
            user32 = ctypes.windll.user32
            user32.SetThreadDpiAwarenessContext.argtypes = [wintypes.HANDLE]
            user32.SetThreadDpiAwarenessContext.restype = wintypes.HANDLE
            user32.SetThreadDpiAwarenessContext(-4)
        except Exception:
            pass
    except Exception:
        pass

import numpy as np
import cv2
import pyautogui
import mss

# Resolve API import - mirror server.py path setup
RESOLVE_API_PATHS = {
    "Windows": [
        r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\Modules",
        r"C:\Program Files\Blackmagic Design\DaVinci Resolve\Developer\Scripting\Modules",
    ],
    "Darwin": [
        "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules",
    ],
    "Linux": [
        "/opt/resolve/Developer/Scripting/Modules",
        os.path.expanduser("~/.local/share/DaVinciResolve/Developer/Scripting/Modules"),
    ],
}
for p in RESOLVE_API_PATHS.get(platform.system(), []):
    if os.path.isdir(p):
        sys.path.insert(0, p)
        break

import DaVinciResolveScript as dvr_script  # noqa: E402

CONFIG_PATH = Path.home() / ".davinci_auto_magic_mask.json"


# ─── Resolve helpers ──────────────────────────────────────────────────────────

def connect_resolve():
    r = dvr_script.scriptapp("Resolve")
    if not r:
        sys.exit("Cannot connect to Resolve. Is it running with external scripting enabled?")
    return r


def get_timeline(resolve):
    proj = resolve.GetProjectManager().GetCurrentProject()
    if not proj:
        sys.exit("No project open.")
    tl = proj.GetCurrentTimeline()
    if not tl:
        sys.exit("No timeline open.")
    return proj, tl


def frame_to_timecode(frame, fps):
    fps_int = int(round(fps))
    h = frame // (fps_int * 3600)
    m = (frame // (fps_int * 60)) % 60
    s = (frame // fps_int) % 60
    f = frame % fps_int
    return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"


def move_playhead_to_clip(tl, item, fps):
    """Position playhead at clip middle. Returns (target_tc, actual_tc_after_set)."""
    target = item.GetStart() + item.GetDuration() // 2
    tc = frame_to_timecode(target, fps)
    tl.SetCurrentTimecode(tc)
    actual = tl.GetCurrentTimecode()
    return tc, actual


def read_source_frame_at_clip_middle(item):
    """Read the source-file frame corresponding to the middle of this timeline clip.

    Returns (rgb_image, (source_w, source_h), src_path) or (None, None, None).
    Bypasses Resolve's viewer entirely — no race condition with the 4K HEVC decoder
    and no need to wait for the Color-page viewer to refresh.
    """
    mpi = item.GetMediaPoolItem()
    if not mpi:
        return None, None, None
    src_path = mpi.GetClipProperty("File Path")
    if not src_path or not Path(src_path).exists():
        return None, None, src_path
    left_offset = item.GetLeftOffset()        # source frame at clip in-point
    duration = item.GetDuration()             # length in timeline frames
    target_frame = left_offset + duration // 2

    cap = cv2.VideoCapture(src_path)
    if not cap.isOpened():
        return None, None, src_path
    sw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    sh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
    ret, bgr = cap.read()
    cap.release()
    if not ret:
        return None, (sw, sh), src_path
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB), (sw, sh), src_path


def map_source_to_screen(face_src, source_size, viewer_rect):
    """Map a (face_x, face_y) point in source-frame coords to absolute screen coords
    inside the Resolve viewer, assuming the source fills the viewer with
    aspect-ratio-preserving fit (letterbox or pillarbox)."""
    fsx, fsy = face_src
    sw, sh = source_size
    vx, vy, vw, vh = viewer_rect
    src_aspect = sw / sh
    view_aspect = vw / vh
    if src_aspect > view_aspect:
        # Source is wider than viewer -> fill width, letterbox top/bottom
        disp_w = vw
        disp_h = vw / src_aspect
        off_x = 0.0
        off_y = (vh - disp_h) / 2.0
    else:
        # Source is taller -> fill height, pillarbox left/right
        disp_h = vh
        disp_w = vh * src_aspect
        off_y = 0.0
        off_x = (vw - disp_w) / 2.0
    abs_x = vx + off_x + (fsx / sw) * disp_w
    abs_y = vy + off_y + (fsy / sh) * disp_h
    return int(round(abs_x)), int(round(abs_y))


# ─── Resolve window locator ───────────────────────────────────────────────────

def find_resolve_window(verbose=False):
    """Return (left, top, width, height) of DaVinci Resolve main window, or None."""
    if platform.system() != "Windows":
        raise NotImplementedError("Window finder currently Windows-only (pywinauto)")
    from pywinauto import Desktop
    candidates = []
    for w in Desktop(backend="uia").windows():
        try:
            title = w.window_text()
            r = w.rectangle()
            wd, ht = r.right - r.left, r.bottom - r.top
            if verbose:
                print(f"    [win] {wd}x{ht} '{title}'")
            # Match anything containing 'Resolve' and reasonably sized
            if "Resolve" in title and wd > 800 and ht > 600:
                candidates.append((wd * ht, w, r))
        except Exception:
            continue
    if not candidates:
        return None
    # Largest wins (the main editor window, not splash/dialogs)
    candidates.sort(reverse=True, key=lambda c: c[0])
    _, w, r = candidates[0]
    try:
        w.set_focus()
    except Exception:
        pass
    return (r.left, r.top, r.right - r.left, r.bottom - r.top)


def estimate_viewer_rect(window_rect):
    """Heuristic Color-page viewer location: center, upper portion of window.

    Color page layout (left to right):
      - Nodes panel: ~310px
      - Viewer (what we want)
      - Gallery / scopes panel: ~330px
    Top: menu+toolbar ~100px. Bottom: clips strip + transport ~280px.
    """
    x, y, w, h = window_rect
    # Assume ~310px node panel left, ~330px gallery right
    nodes_w = 310
    gallery_w = 330
    # Top toolbar ~ 100px, bottom ~ 280px
    top_pad = 100
    bottom_pad = 280

    vx = x + nodes_w
    vy = y + top_pad
    vw = max(0, w - nodes_w - gallery_w)
    vh = max(0, h - top_pad - bottom_pad)

    # Pad inward to avoid clicking edge UI
    vx += 20
    vy += 20
    vw -= 40
    vh -= 40
    return (vx, vy, vw, vh)


def load_calibration():
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except Exception:
            return None
    return None


def save_calibration(viewer_rect):
    CONFIG_PATH.write_text(json.dumps({"viewer_rect": list(viewer_rect)}, indent=2))
    print(f"Saved calibration to {CONFIG_PATH}")


# ─── Screenshot ───────────────────────────────────────────────────────────────

def grab(rect):
    """Return RGB numpy array of region (left, top, w, h).

    Uses PIL ImageGrab(all_screens=True) inside per_monitor_aware context so
    coords are in physical pixels of the virtual desktop — matching cursor
    coords also taken in per_monitor_aware context. Critical for multi-monitor
    + mixed-DPI setups.
    """
    from PIL import ImageGrab
    x, y, w, h = rect
    with _per_monitor_aware():
        pil_img = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
    rgb = np.array(pil_img.convert("RGB"))
    return rgb


def _cursor_pos():
    """pyautogui.position() under per_monitor_aware so coords are physical px."""
    with _per_monitor_aware():
        return pyautogui.position()


def _click_at(x, y):
    """Move + click at physical-pixel coords under per_monitor_aware."""
    with _per_monitor_aware():
        pyautogui.moveTo(x, y, duration=0.15)
        pyautogui.click()


def _diag_dpi():
    """One-time diagnostic dump of DPI / monitor / coord-system state.

    Shows BOTH the default thread awareness and what we get inside
    _per_monitor_aware() — should be different on a system with locked
    process awareness.
    """
    import ctypes
    def _get_awareness():
        try:
            return ctypes.windll.user32.GetAwarenessFromDpiAwarenessContext(
                ctypes.windll.user32.GetThreadDpiAwarenessContext()
            )
        except Exception as e:
            return f"error: {e}"

    print(f"  thread awareness (default):     {_get_awareness()}")
    with _per_monitor_aware():
        print(f"  thread awareness (under PMv2):  {_get_awareness()}")
        print(f"    pyautogui.size:               {pyautogui.size()}")
        print(f"    cursor pos:                   {pyautogui.position()}")
    print(f"  pyautogui.size (default):       {pyautogui.size()}")
    try:
        with mss.mss() as sct:
            print(f"  mss.monitors: {sct.monitors}")
    except Exception as e:
        print(f"  mss.monitors error: {e}")


# ─── Face detection ───────────────────────────────────────────────────────────

MODEL_PATH = Path(__file__).parent / "blaze_face_short_range.tflite"


class FaceFinder:
    """Mediapipe BlazeFace detector (short-range model, ~2m).

    Returns the center of the highest-scoring face, or None if no face found.
    Much more reliable than Haar cascades for real-world footage — doesn't
    false-trigger on clothing/textures the way cascades do.
    """

    def __init__(self):
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision as mp_vision
        if not MODEL_PATH.exists():
            raise FileNotFoundError(
                f"Missing face detection model: {MODEL_PATH}. "
                "Re-download from "
                "https://storage.googleapis.com/mediapipe-models/face_detector/"
                "blaze_face_short_range/float16/1/blaze_face_short_range.tflite"
            )
        base_options = mp_python.BaseOptions(model_asset_path=str(MODEL_PATH))
        options = mp_vision.FaceDetectorOptions(
            base_options=base_options,
            min_detection_confidence=0.4,
        )
        self._detector = mp_vision.FaceDetector.create_from_options(options)
        self._mp = __import__("mediapipe")

    def detect(self, rgb_img):
        """Return (cx, cy) of highest-scoring face, or None."""
        mp_image = self._mp.Image(
            image_format=self._mp.ImageFormat.SRGB,
            data=rgb_img,
        )
        result = self._detector.detect(mp_image)
        if not result.detections:
            return None
        # Pick highest-confidence detection
        best = max(
            result.detections,
            key=lambda d: d.categories[0].score if d.categories else 0.0,
        )
        bbox = best.bounding_box  # origin_x, origin_y, width, height (pixels)
        cx = bbox.origin_x + bbox.width // 2
        cy = bbox.origin_y + bbox.height // 2
        return (cx, cy)


# ─── Calibration ──────────────────────────────────────────────────────────────

def _capture_corner_thumb(x, y, label, debug_dir, half=80):
    """Save a small thumb centered on (x, y) — lets us verify visually where
    the cursor actually was when the user pressed Enter."""
    rect = (x - half, y - half, half * 2, half * 2)
    try:
        img = grab(rect)
        path = debug_dir / f"corner_{label}.jpg"
        # Mark the centerpoint with a crosshair
        annotated = cv2.cvtColor(img, cv2.COLOR_RGB2BGR).copy()
        cv2.line(annotated, (half, half - 30), (half, half + 30), (0, 0, 255), 2)
        cv2.line(annotated, (half - 30, half), (half + 30, half), (0, 0, 255), 2)
        cv2.imwrite(str(path), annotated)
        return path
    except Exception as e:
        print(f"  thumb capture failed: {e}")
        return None


def calibrate_viewer(window_rect):
    """Interactive: user points the mouse at the visible clip-image corners."""
    debug_dir = Path(__file__).parent
    print("\n=== CALIBRATION ===")
    print("Diagnostics:")
    _diag_dpi()
    print()
    print("In Resolve: Color page open, a clip displayed in the viewer,")
    print("Magic Mask palette open.\n")
    print("STEP 1/2: Move mouse to TOP-LEFT corner of the visible clip image")
    print("          area in the viewer. Then press Enter.")
    input(">>> ")
    tl = _cursor_pos()
    tl_path = _capture_corner_thumb(tl.x, tl.y, "TL", debug_dir)
    print(f"  TL = ({tl.x}, {tl.y})  thumb: {tl_path}")

    print("\nSTEP 2/2: Move mouse to BOTTOM-RIGHT corner of the visible clip")
    print("          image area. Then press Enter.")
    input(">>> ")
    br = _cursor_pos()
    br_path = _capture_corner_thumb(br.x, br.y, "BR", debug_dir)
    print(f"  BR = ({br.x}, {br.y})  thumb: {br_path}")

    if br.x <= tl.x or br.y <= tl.y:
        sys.exit("Invalid viewer corners — BR must be below+right of TL.")

    rect = (tl.x, tl.y, br.x - tl.x, br.y - tl.y)
    print(f"\nViewer rect: {rect}  ({rect[2]}x{rect[3]})")

    img = grab(rect)
    debug_path = debug_dir / "calibration_screenshot.jpg"
    cv2.imwrite(str(debug_path), cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
    print(f"\nFull-rect screenshot:  {debug_path}")
    print(f"TL corner thumb:       {tl_path}")
    print(f"BR corner thumb:       {br_path}")
    print("\nVerification:")
    print("  • TL/BR thumbs: red crosshair on intended corners of the clip image.")
    print("  • Full-rect screenshot: only the clip image, no UI panels.")
    print("\nKeep this calibration? [y/N]")
    answer = input("> ").strip().lower()
    if answer != "y":
        sys.exit("Calibration not saved.")
    save_calibration(rect)
    return rect


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--calibrate", action="store_true", help="Calibrate viewer rect interactively")
    p.add_argument("--clips", default="all", help="Comma-separated indices or 'all'")
    p.add_argument("--dry-run", action="store_true", help="No click/trigger, just report")
    p.add_argument("--debug", action="store_true", help="Save annotated screenshots per clip")
    p.add_argument("--debug-dir", default="auto_mm_debug", help="Where to save debug images")
    p.add_argument("--seed-delay", type=float, default=1.0, help="Seconds to wait after click")
    p.add_argument("--playhead-delay", type=float, default=0.6, help="Seconds to wait after seeking")
    p.add_argument("--no-prompt", action="store_true",
                   help="Skip the 'Press Enter when ready' pre-flight prompt (for MCP/automation)")
    args = p.parse_args()

    print("Connecting to Resolve...")
    resolve = connect_resolve()
    print(f"  {resolve.GetProductName()} {resolve.GetVersionString()}")

    proj, tl = get_timeline(resolve)
    items = tl.GetItemListInTrack("video", 1)
    fps = float(proj.GetSetting("timelineFrameRate") or 25)
    print(f"  Timeline '{tl.GetName()}': {len(items)} clips @ {fps} fps")

    if args.calibrate:
        wrect = find_resolve_window()
        if not wrect:
            sys.exit("Can't find Resolve window")
        calibrate_viewer(wrect)
        return

    # Resolve clips selection
    if args.clips.lower() == "all":
        indices = list(range(len(items)))
    else:
        indices = [int(x) for x in args.clips.split(",") if x.strip()]

    # Window + viewer rect — required for clicking, optional in dry-run
    print("\nLocating Resolve window...")
    wrect = find_resolve_window(verbose=False)
    if not wrect:
        print("  [!] Resolve window not found via pywinauto. Listing all windows:")
        find_resolve_window(verbose=True)
        if not args.dry_run:
            sys.exit("Can't find Resolve window — required for clicking. "
                     "Run with --dry-run --debug to test face detection only.")
        viewer_rect = (0, 0, 1920, 1080)  # placeholder for dry-run math
        print("  [dry-run] using placeholder viewer_rect — click coords will be meaningless")
    else:
        print(f"  Window: {wrect}")
        cal = load_calibration()
        if cal:
            viewer_rect = tuple(cal["viewer_rect"])
            print(f"  Viewer (calibrated): {viewer_rect}")
        else:
            viewer_rect = estimate_viewer_rect(wrect)
            print(f"  Viewer (estimated): {viewer_rect}")
            print("  Use --calibrate for a tighter rect if face detection misses.")

    print("\n[!] PRE-FLIGHT - do this NOW in Resolve:")
    print("    1. Switch to Color page (Shift+6 or click 'Color' tab)")
    print("    2. Open Magic Mask palette")
    print("    3. Set mode to 'People & Object', subject 'Person'")
    if not args.dry_run and not args.no_prompt:
        print("Press Enter when ready...")
        input()

    # Open Color page programmatically
    resolve.OpenPage("color")
    time.sleep(0.3)

    # Setup
    debug_dir = Path(args.debug_dir)
    if args.debug:
        debug_dir.mkdir(exist_ok=True)
    finder = FaceFinder()
    pyautogui.PAUSE = 0.05  # small pause between pyautogui calls

    success = 0
    skipped = 0
    failed = 0

    for idx in indices:
        if idx >= len(items):
            print(f"\n[{idx}] out of range — skip")
            skipped += 1
            continue
        item = items[idx]
        name = item.GetName()
        print(f"\n[{idx:2d}] {name}  (start_frame={item.GetStart()}, dur={item.GetDuration()})")

        # 1) Read source frame DIRECTLY (independent of viewer state)
        rgb, src_size, src_path = read_source_frame_at_clip_middle(item)
        if rgb is None:
            print(f"  cannot read source frame: {src_path}")
            skipped += 1
            continue
        print(f"  source: {src_size[0]}x{src_size[1]} from {Path(src_path).name}")

        face = finder.detect(rgb)
        if not face:
            print("  no face detected in source — skip")
            if args.debug:
                cv2.imwrite(str(debug_dir / f"clip_{idx:02d}_noface_src.jpg"),
                            cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
            skipped += 1
            continue
        fsx, fsy = face
        print(f"  face at source({fsx},{fsy})")

        # 2) Move playhead so the viewer shows the same frame for the click
        try:
            target_tc, actual_tc = move_playhead_to_clip(tl, item, fps)
            print(f"  playhead set: target={target_tc} actual={actual_tc}")
        except Exception as e:
            print(f"  playhead error: {e}")
        time.sleep(args.playhead_delay)

        # 3) Map source-frame face coords to viewer screen coords
        abs_x, abs_y = map_source_to_screen((fsx, fsy), src_size, viewer_rect)
        print(f"  -> click screen ({abs_x},{abs_y})")

        if args.debug:
            dbg = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR).copy()
            cv2.circle(dbg, (fsx, fsy), 60, (0, 255, 0), 8)
            cv2.line(dbg, (fsx - 100, fsy), (fsx + 100, fsy), (0, 255, 0), 4)
            cv2.line(dbg, (fsx, fsy - 100), (fsx, fsy + 100), (0, 255, 0), 4)
            # Resize debug to readable size
            scale = 1280 / dbg.shape[1]
            dbg_small = cv2.resize(dbg, (1280, int(dbg.shape[0] * scale)))
            cv2.imwrite(str(debug_dir / f"clip_{idx:02d}_seed_src.jpg"), dbg_small)

        if args.dry_run:
            print("  [dry-run] would click + CreateMagicMask")
            success += 1
            continue

        # Click on viewer at the mapped coords (physical-pixel coords)
        _click_at(abs_x, abs_y)
        time.sleep(args.seed_delay)

        # Trigger Magic Mask via API
        result = item.CreateMagicMask("BI")
        print(f"  CreateMagicMask -> {result}")
        if result:
            success += 1
        else:
            failed += 1

    print(f"\n=== {success} ok, {skipped} skipped, {failed} failed ===")


if __name__ == "__main__":
    main()
