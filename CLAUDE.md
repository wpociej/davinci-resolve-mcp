# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

DaVinci Resolve MCP Server — provides complete coverage of the DaVinci Resolve Scripting API via MCP (Model Context Protocol). Two server modes: compound (~29 tools) and full/granular (342 tools). Plus first-party plugins under `tools/` that use the API for higher-level workflows.

## Run / Develop / Test Commands

The project ships its own venv. All commands assume Windows paths; on macOS/Linux replace `venv/Scripts/` with `venv/bin/`.

```bash
# Run the compound server (default — ~29 action-dispatch tools)
venv/Scripts/python.exe src/server.py

# Run the full granular server (342 one-method-per-tool)
venv/Scripts/python.exe src/server.py --full

# Reinstall + (re)write .mcp.json for clients
PYTHONIOENCODING=utf-8 python install.py --clients claude-code
# PYTHONIOENCODING=utf-8 is required on Windows + Polish locale (cp1250)
# otherwise the installer banner crashes on box-drawing characters.

# Run the live test suites (Resolve Studio must be running)
venv/Scripts/python.exe tests/test_all_tools.py
venv/Scripts/python.exe tests/test_phase4_remaining.py     # specific phase
venv/Scripts/python.exe tests/test_live_api.py             # raw API smoke

# Plugins under tools/ — see tools/<name>/README.md for usage
venv/Scripts/python.exe tools/auto_magic_mask/auto_magic_mask.py --calibrate
venv/Scripts/python.exe tools/auto_magic_mask/auto_grade.py --help
```

## Architecture

### Two-server pattern

The repo ships **two MCP server entry points** that share path-setup, logging, and the lazy Resolve-connection logic at the top of each file:

- **`src/server.py`** — compound server. Each `@mcp.tool()` is an action-dispatcher: takes an `action` string + optional `params: dict` and routes to the underlying API call. Goal: fewer tools, easier for an LLM to discover. Contains the `execute_python` escape hatch.
- **`src/resolve_mcp_server.py`** — full server. One MCP tool per Resolve API method. ~342 tools. Used when an LLM needs precise method-level control or when the compound dispatcher's `_unknown(action, ...)` would reject a valid call.

Both share helpers from `src/utils/`. The compound server uses `_check()`, `_err()`, `_ok()`, `_get_mp()`, `_get_tl()`, `_resolve_safe_dir()`. Helpers prefixed `_` are intentionally private and not exported.

### Lazy connection

`get_resolve()` connects to Resolve **on first tool call**, not at server startup. This means the server starts instantly even if Resolve is closed. First call may take up to 60 s while Resolve launches and the Scripting API initializes. See `_try_connect()` and `_launch_resolve()` in `src/server.py`.

### Action-dispatch pattern (compound tools)

Each compound tool follows the same shape:

```python
@mcp.tool()
def media_pool(action: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    p = params or {}
    pm, proj, err = _check()
    if err:
        return err
    if action == "import_media":
        return _ok(...)
    elif action == "create_empty_timeline":
        return _ok(...)
    return _unknown(action, [...])
```

To add a new action to an existing compound tool: add an `elif` branch and append the action name to the `_unknown()` list. To add a new compound tool: copy one of the existing tool functions (lines 276–322 are good templates) and register the actions you need.

### `execute_python` escape hatch

`src/server.py` line ~2533. Auto-injects `resolve`, `project_manager`, `project`, `media_pool`, `timeline`, `fusion`, `dvr_script` as globals. Captures `print()` and returns the last expression's value. Use this when:
- A compound tool doesn't cover what you need
- Discovering the API surface (`dir(item)` in a live Resolve)
- One-shot exploratory work

It runs arbitrary Python with full process privileges — never pass untrusted input to it.

### Plugins under `tools/`

Self-contained scripts that use the Resolve Scripting API plus other libraries (mediapipe, pyautogui, pywinauto, ffmpeg) for higher-level workflows that the API alone can't do.

- `tools/auto_magic_mask/auto_magic_mask.py` — auto-clicker for Magic Mask Person seeding. Reads source frames directly via `cv2.VideoCapture`, detects faces with mediapipe BlazeFace, maps source-frame coords to Resolve viewer screen coords, clicks via pyautogui, calls `CreateMagicMask`. Required because `CreateMagicMask` API needs a manual viewer click before it can track.
- `tools/auto_magic_mask/auto_grade.py` — adds serial/outside nodes via Alt+S / Alt+O keyboard shortcuts (Resolve API has no `AddNode` method) and applies CDL on the new nodes. Used for batch warm-grade across a timeline.

Both plugins are also exposed as MCP tools (`auto_seed_magic_mask` in `src/server.py`) that subprocess them. Plugins do their own DPI awareness via `SetThreadDpiAwarenessContext(-4)` — see "Hard-won pitfalls" below.

### Sandbox path safety

Resolve cannot write to many platform sandbox paths (`/var/folders` on macOS, `AppData\Local\Temp` on Windows, `/tmp` on Linux). All temp paths Resolve will write to **must** go through `_resolve_safe_dir()` in `src/server.py`, which redirects to `~/Documents/resolve-stills` (or platform equivalent). Never use `tempfile.gettempdir()` directly for paths Resolve touches.

## Hard-won pitfalls

These bugs and limitations are real and reproducible. Encode them when extending the server or plugins.

### Resolve 21.0.0b.28 SetCDL Slope-only-red bug

`item.SetCDL({"NodeIndex": "1", "Slope": "0.97 0.97 0.97"})` produces a strong **red cast** because the slope is applied to the red channel only. Workaround: use `Slope: "1.0 1.0 1.0"` (identity) and rely on `Offset` / `Power` / `Saturation` for the look. Or use ffmpeg post-process for exposure matching outside Resolve. Always verify visually after any CDL write — don't trust that `SetCDL` returning `True` means the result is correct.

### Resolve API has no `AddNode` / no key-wire methods

Neither `dir(timelineItem)` nor `dir(graph)` exposes any way to add a node, delete a node, or wire alpha-key connections between nodes. The only way to add nodes programmatically is via keyboard shortcuts (`Alt+S` / `Alt+O` / `Alt+L` / `Alt+P`) sent through `pyautogui` to a focused Resolve window — see `tools/auto_magic_mask/auto_grade.py`. Even then, `Alt+S` does **not** auto-wire the alpha key from upstream nodes, so a Magic Mask on Node 1 will not gate a grade on Node 2 unless the user manually drags the key wire in the GUI. This is the platform's documented limitation.

### `CreateTimelineFromClips` adds gaps between clips

Confirmed in this codebase — produces 15–62 frame gaps that destroy beat alignment. Use `mp.CreateEmptyTimeline(name)` + `mp.AppendToTimeline(clip_infos)` with explicit `recordFrame` per clip instead.

### `SetTrackEnable("audio", 1, False)` doesn't mute source audio

The flag affects only UI visibility, not the master mix. To kill linked source audio: `tl.DeleteClips(tl.GetItemListInTrack("audio", 1), False)`.

### Magic Mask `CreateMagicMask("BI")` requires GUI seed click

Returns `False` silently if no click was made in the viewer first. Pre-flight: Color page open + Magic Mask palette open + tracker mode set. The `auto_seed_magic_mask` MCP tool / `auto_magic_mask.py` plugin automates this click.

### NodeIndex is 1-based since v16.2.0

Common off-by-one when porting older scripts. SetCDL/SetLUT — pass 1 for the first node, never 0.

### Windows + Python launcher locks DPI awareness

`SetProcessDpiAwarenessContext(-4)` silently no-ops because Python's launcher manifest pre-sets DPI awareness. Use `SetThreadDpiAwarenessContext(-4)` per-thread instead — survives locked process awareness. Without this, on multi-monitor + mixed-DPI setups, `pyautogui.position()` returns coords in a virtualized space and screenshots / clicks land in wrong regions. See `_per_monitor_aware()` context manager in `tools/auto_magic_mask/auto_magic_mask.py`.

### Python 3.13+ may break Resolve scripting

Resolve's `DaVinciResolveScript` module is C-extension-bound to specific Python ABIs. 3.13+ tested broken. Stick to **3.10–3.12** for the venv. The server prints a warning at startup if it detects 3.13+.

### Local API doc is outdated (~v18.6/19)

`docs/resolve_scripting_api.txt` is a 998-line snapshot that's missing `RegenerateMagicMask`, `Stabilize`, `SmartReframe`, voice-isolation, `SetColorOutputCache` / `SetFusionOutputCache`, `CreateSubtitlesFromAudio`, `DetectSceneCuts`. The current canonical reference is **v20.3** at https://gist.github.com/X-Raym/2f2bf453fc481b9cca624d7ca0e19de8 and https://extremraym.com/cloud/resolve-scripting-doc/. Resolve 21 betas have not bumped the API surface — v20.3 still applies.

### Magic Mask quality (Better/Faster) and Legacy/New toggle are GUI-only

Confirmed across all sources. `CreateMagicMask(mode)` only accepts mode ∈ {"F","B","BI"} — direction, not quality. The Fusion `MagicMask` node (added in 18.1) IS scriptable and DOES expose Better/Faster, but uses Object segmentation (not Person). For person-specific masks at Better quality, manual GUI is the only path.

## Version Locations

When bumping the version, ALL of these must be updated:

- `src/server.py` → `VERSION = "x.y.z"`
- `src/resolve_mcp_server.py` → `VERSION = "x.y.z"`
- `README.md` → version badge on line 3: `badge/version-x.y.z-blue.svg`
- `README.md` → "What's New in vX.Y.Z" section (add new, demote previous)

## Release Checklist (MANDATORY for every version bump)

Every incremental update MUST include ALL of the following before the commit is made:

1. **Bump `VERSION` in both server files** — `src/server.py` and `src/resolve_mcp_server.py` must match
2. **Update README.md version badge** — the shields.io badge on line 3 must reflect the new version
3. **Update README.md changelog** — add a new "What's New in vX.Y.Z" section at the top, demote the previous version's section to just "### vX.Y.Z"
4. **Update any other badges** if tool count, API coverage, or test percentage changed
5. **After pushing, create/update the GitHub release** — use `gh release create vX.Y.Z` (or `gh release edit`) with:
   - Tag: `vX.Y.Z`
   - Title: `vX.Y.Z`
   - Body: the changelog entry from the README
6. **Verify the release** — `gh release view vX.Y.Z` to confirm it's live

Do NOT commit a version bump without completing steps 1–4. Steps 5–6 happen after push.

## Coding Conventions

- Python 3.10–3.12 (3.13+ broken with Resolve scripting), no type stubs needed
- Helper functions prefixed with `_` (e.g., `_err`, `_ok`, `_check`, `_resolve_safe_dir`)
- Compound server tools use action-based dispatch pattern with `_unknown(action, [...])` as the fall-through
- All temp/sandbox paths must go through `_resolve_safe_dir()` — never use `tempfile.gettempdir()` directly for paths Resolve will write to
- Plugins under `tools/<name>/` are self-contained: own README.md, own files, own dependencies handled in the server's venv
- Commit messages use conventional format: `feat:`, `fix:`, `docs:`, `security:`, `refactor:`
- API discovery for unknown methods: use `execute_python` with `dir(item)` rather than guessing — Resolve's API surface evolves between versions and the official PDF lags
