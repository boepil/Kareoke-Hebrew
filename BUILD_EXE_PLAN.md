# Karaoke Editor — Windows .exe Build Plan

## Goal
Create a standalone Windows `.exe` (via installer) of the full pipeline editor, **excluding** the AI first-pass (LM Studio correction).  

**Desktop wrapper:** `pywebview` — native WebView2 window (Edge Chromium, pre-installed on Win10+).  
**Model weights:** Downloaded on first pipeline run (~5 GB to `~/.cache/`).

---

## Files to create

| File | Purpose |
|------|---------|
| `pyproject.toml` | Package metadata + `[project.scripts]` entry point |
| `win_run.py` | Entry point — starts Flask on background thread, opens `pywebview` window |
| `timing_editor.spec` | PyInstaller build config |
| `installer.iss` | Inno Setup installer script |

## Files to modify

| File | Change |
|------|--------|
| `timing_editor.py` | New route guard: `DISABLE_AI_FIRST_PASS=1` disables AI Pass endpoint; injects `<meta name="no-ai">` into served HTML |
| `ui/timing_editor.html` | Check for `no-ai` meta tag on init; hide `.sidebar-section-ai` if present |
| `config.yaml` | Ship with `autosync.correction_enabled: false` |

## What's excluded from the bundle

- `modules/first_pass.py` — not bundled; endpoint returns 404
- `modules/lyrics_corrector.py` — not bundled (LM Studio dependency)
- Model weights (`whisper`, `demucs`, `whisperX`) — downloaded to `~/.cache/` on first run
- CUDA/cuDNN — CPU-only PyTorch to keep bundle smaller

## Architecture

```
┌─────────────────────────────────────┐
│  PyInstaller bundle (single folder) │
│  ┌───────────────────────────────┐  │
│  │ python/ (embedded runtime)    │  │
│  │ site-packages/ (CPU torch +   │  │
│  │   flask, demucs, whisper,     │  │
│  │   whisperx, etc.)             │  │
│  │ timing_editor.exe             │  │
│  │ ffmpeg.exe                    │  │
│  │ ui/timing_editor.html         │  │
│  │ config.yaml                   │  │
│  └───────────────────────────────┘  │
│                                     │
│  pywebview (bundled in exe)         │
│  → native Windows window            │
│  → loads http://127.0.0.1:8765      │
└─────────────────────────────────────┘
```

## Build steps (on dev machine)

```cmd
pip install pyinstaller pywebview
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt
pyinstaller timing_editor.spec
iscc installer.iss
```

**Output:** `Output/setup.exe` (~600 MB compressed).

## Size breakdown (estimate)

| Component | Size |
|-----------|------|
| Python + pip packages (CPU torch) | ~900 MB |
| Flask + app code + UI | ~30 MB |
| FFmpeg | ~80 MB |
| Overhead | ~190 MB |
| **PyInstaller bundle** | **~1.2 GB** |
| Inno Setup compressed installer | **~500–600 MB** |

## User experience

1. Run the installer (`setup.exe`)
2. Desktop shortcut created
3. Double-click → native Windows window opens (WebView2) showing the editor
4. First pipeline run downloads models automatically (progress shown in UI)
5. AI Pass button is hidden — everything else (YouTube import, audio upload, vocal separation, transcription, alignment, render) works normally

## Files that remain unchanged

- `modules/aligner.py`
- `modules/audio_extractor.py`
- `modules/editor_project.py`
- `modules/renderer.py`
- `modules/separator.py`
- `modules/subtitle_builder.py`
- `modules/transcriber.py`
- `modules/lyrics_source.py`
- `tests/` (all)
- `data/`, `logs/` (created at runtime)

## What the plan does NOT cover

- Signing the executable (code signing certificate)
- Auto-update mechanism
- macOS / Linux builds
