"""auto_grade.py — apply per-mask grade to every clip with Magic Mask.

Workflow per clip:
  1. Move playhead to clip middle
  2. Focus Resolve, send Alt+S (Add Serial Node) — if clip has only 1 node
  3. SetCDL on Node 2 with warm/saturation offset

The serial Node 2 inherits Node 1's alpha key (Magic Mask), so the grade
applies only to the masked subject.

Usage:
  python auto_grade.py                           # default warm grade, all clips
  python auto_grade.py --clips 1,3,5             # specific clips
  python auto_grade.py --offset "0.03 0.01 -0.015" --saturation 1.08
  python auto_grade.py --reset                   # clear Node 2 CDL on all clips
"""
import argparse
import os
import platform
import sys
import time
from pathlib import Path

# DPI awareness — same as auto_magic_mask.py
if platform.system() == "Windows":
    try:
        import ctypes
        from ctypes import wintypes
        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)
        except Exception:
            pass
        try:
            user32 = ctypes.windll.user32
            user32.SetThreadDpiAwarenessContext.argtypes = [wintypes.HANDLE]
            user32.SetThreadDpiAwarenessContext.restype = wintypes.HANDLE
            user32.SetThreadDpiAwarenessContext(-4)
        except Exception:
            pass
    except Exception:
        pass

import pyautogui

# Resolve API
api_paths = [
    r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\Modules",
    r"C:\Program Files\Blackmagic Design\DaVinci Resolve\Developer\Scripting\Modules",
]
for p in api_paths:
    if os.path.isdir(p):
        sys.path.insert(0, p)
        break
import DaVinciResolveScript as dvr_script  # noqa: E402


def focus_resolve():
    if platform.system() != "Windows":
        return False
    try:
        from pywinauto import Desktop
        candidates = []
        for w in Desktop(backend="uia").windows():
            try:
                title = w.window_text()
                r = w.rectangle()
                wd, ht = r.right - r.left, r.bottom - r.top
                if "Resolve" in title and wd > 800 and ht > 600:
                    candidates.append((wd * ht, w))
            except Exception:
                continue
        if not candidates:
            return False
        candidates.sort(reverse=True, key=lambda c: c[0])
        candidates[0][1].set_focus()
        return True
    except Exception:
        return False


def frame_to_tc(frame, fps):
    fps_int = int(round(fps))
    h = frame // (fps_int * 3600)
    m = (frame // (fps_int * 60)) % 60
    s = (frame // fps_int) % 60
    f = frame % fps_int
    return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"


def add_node_via_shortcut(item, item_idx, key="s"):
    """Add a node via keyboard shortcut. key='s' -> serial (inherits key),
    key='o' -> outside (inverted key). Returns True if NumNodes increased."""
    nodes_before = item.GetNumNodes()
    if not focus_resolve():
        print(f"  [!] focus failed for clip {item_idx}")
        return False
    time.sleep(0.35)
    pyautogui.hotkey("alt", key)
    time.sleep(0.6)
    nodes_after = item.GetNumNodes()
    if nodes_after <= nodes_before:
        print(f"  [!] Alt+{key.upper()} didn't add a node ({nodes_before} -> {nodes_after})")
        return False
    return True


def add_serial_node(item, item_idx):
    return add_node_via_shortcut(item, item_idx, "s")


def add_outside_node(item, item_idx):
    return add_node_via_shortcut(item, item_idx, "o")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--clips", default="all",
                   help="comma-separated indices, 'all', or 'masked' (clips with >0 nodes)")
    p.add_argument("--slope", default="1.0 1.0 1.0", help='space-separated R G B')
    p.add_argument("--offset", default="0.02 0.005 -0.01",
                   help='warm push: R+ G+slight B- ; default = warm "dreamy family"')
    p.add_argument("--power", default="1.0 1.0 1.0", help='space-separated R G B')
    p.add_argument("--saturation", default="1.05", help='global saturation multiplier')
    p.add_argument("--reset", action="store_true",
                   help="reset target node CDL to identity (no grade)")
    p.add_argument("--node-index", type=int, default=2,
                   help="which node to grade (default 2 = serial after Magic Mask)")
    p.add_argument("--outside", action="store_true",
                   help="use Alt+O (outside node, inverted key) instead of Alt+S. "
                        "Adds a node that affects everything OUTSIDE the Magic Mask. "
                        "Useful for background-only grade (e.g. desaturation).")
    p.add_argument("--no-prompt", action="store_true",
                   help="skip pre-flight prompt (for MCP)")
    args = p.parse_args()

    if args.reset:
        args.slope = "1.0 1.0 1.0"
        args.offset = "0.0 0.0 0.0"
        args.power = "1.0 1.0 1.0"
        args.saturation = "1.0"

    print("Connecting to Resolve...")
    r = dvr_script.scriptapp("Resolve")
    proj = r.GetProjectManager().GetCurrentProject()
    tl = proj.GetCurrentTimeline()
    fps = float(proj.GetSetting("timelineFrameRate") or 25)
    items = tl.GetItemListInTrack("video", 1)
    print(f"  Timeline '{tl.GetName()}': {len(items)} clips @ {fps} fps")

    # Pick indices
    if args.clips == "all":
        indices = list(range(len(items)))
    elif args.clips == "masked":
        indices = [i for i, it in enumerate(items) if it.GetNumNodes() > 0]
    else:
        indices = [int(x) for x in args.clips.split(",") if x.strip()]

    print(f"\nCDL on Node {args.node_index}:")
    print(f"  Slope:      {args.slope}")
    print(f"  Offset:     {args.offset}")
    print(f"  Power:      {args.power}")
    print(f"  Saturation: {args.saturation}")
    print(f"\nClips: {indices}")

    if not args.no_prompt:
        print("\nPre-flight: Color page open, no manual node tweaks in progress.")
        print("Plugin will move playhead per clip and Alt+S as needed.")
        input("Press Enter to start... ")

    r.OpenPage("color")
    time.sleep(0.3)

    success = 0
    failed = 0
    skipped = 0
    pyautogui.PAUSE = 0.05

    for idx in indices:
        if idx >= len(items):
            print(f"\n[{idx}] out of range — skip")
            skipped += 1
            continue
        item = items[idx]
        name = item.GetName()
        nodes = item.GetNumNodes()
        print(f"\n[{idx:2d}] {name}  (nodes={nodes})")

        if nodes == 0:
            print("  no nodes (no Magic Mask seeded?) — skip")
            skipped += 1
            continue

        # Move playhead to clip middle (so Resolve UI shows this clip)
        target = item.GetStart() + item.GetDuration() // 2
        tc = frame_to_tc(target, fps)
        tl.SetCurrentTimecode(tc)
        time.sleep(0.4)

        # Decide target node:
        #   --outside: always add a NEW outside node (inverted key) and grade it
        #   normal:    add serial nodes until we reach --node-index
        if args.outside:
            if not add_outside_node(item, idx):
                print("  failed to add outside node — skip")
                failed += 1
                continue
            target_node = item.GetNumNodes()  # the just-added node
            print(f"  added outside node -> Node {target_node}")
        else:
            target_node = args.node_index
            if nodes < target_node:
                ok = True
                for _ in range(target_node - nodes):
                    if not add_serial_node(item, idx):
                        print("  failed to add serial node — skip")
                        failed += 1
                        ok = False
                        break
                if not ok:
                    continue
                print(f"  added {target_node - nodes} serial node(s)")
            if item.GetNumNodes() < target_node:
                failed += 1
                continue

        # Apply CDL on target node
        result = item.SetCDL({
            "NodeIndex": str(target_node),
            "Slope": args.slope,
            "Offset": args.offset,
            "Power": args.power,
            "Saturation": args.saturation,
        })
        print(f"  SetCDL Node {target_node} -> {result}")
        if result:
            success += 1
        else:
            failed += 1

    print(f"\n=== {success} ok, {skipped} skipped, {failed} failed ===")


if __name__ == "__main__":
    main()
