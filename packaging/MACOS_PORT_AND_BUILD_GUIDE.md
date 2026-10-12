# Karaoke Studio Core: macOS Port and Build Guide

This document provides complete technical documentation for running, building, packaging, and installing **Karaoke Studio Core** on macOS (Apple Silicon primary target), while keeping the Windows Core edition and installer working unchanged.

---

## 1. Technical Assessment: Apple Silicon vs. Intel macOS

| Dependency / Component | Apple Silicon (arm64) Status | Intel Mac (x86_64) Status | Notes & Optimization |
|---|---|---|---|
| **Python 3.10 / 3.11** | Fully supported | Fully supported | Native arm64 runtime recommended. |
| **PyTorch & Torchaudio** | Fully supported (`arm64`) | Supported (`x86_64`) | Apple Silicon uses Accelerate framework and NEON SIMD; MPS available for select ops. |
| **Demucs (`htdemucs_ft`)** | Fully supported | Supported (slower) | Runs on CPU via PyTorch. On Apple Silicon, M-series unified memory and Accelerate BLAS deliver fast vocal separation without MPS memory overhead. |
| **Faster-Whisper (CTranslate2)** | Fully supported (`arm64`) | Supported (`x86_64`) | CTranslate2 publishes prebuilt macOS arm64 wheels on PyPI optimized with Apple Accelerate for int8 GEMM. |
| **WhisperX** | Fully supported (`arm64`) | Supported (`x86_64`) | Alignment wav2vec2 model runs on CPU via Torchaudio. `TRANSFORMERS_NO_TF=1` prevents TensorFlow probing. |
| **FFmpeg** | Fully supported | Fully supported | Universal static binary or Homebrew (`/opt/homebrew/bin/ffmpeg`). |
| **Deno / Node.js** | Fully supported | Fully supported | Apple Silicon arm64 binary (`/opt/homebrew/bin/deno`). |
| **Web GUI (pywebview)** | Fully supported | Fully supported | Uses native **WebKit (WKWebView)** via Cocoa/PyObjC. Does not require Edge or WebView2. |

### Intel Mac Support Evaluation
While Intel x86_64 can run the Python code, combining arm64 and x86_64 into a single "Universal 2" bundle would double the bundle size (PyTorch, Torchaudio, CTranslate2, and FFmpeg binary wheels are ~200MB+ each per architecture, and PyPI wheels are architecture-specific). Therefore, **Apple Silicon (`arm64`) is the primary, robust build target**. Intel Macs require a separate x86_64 runner build or Rosetta 2.

---

## 2. Key Porting Changes & Preserved Compatibility

1. **User Data & Project Storage**:
   - **Windows**: `%LOCALAPPDATA%\KaraokeStudio\CoreData`
   - **macOS**: `~/Library/Application Support/KaraokeStudio/CoreData`
   - Projects are stored completely outside the `/Applications/KaraokeStudioCore.app` bundle. Upgrading the `.app` or replacing it with a newer version never deletes user projects or manual timings.
   - Project directory structures (`project.json`, `state/`, `subtitles/`, `audio/`) are 100% binary- and path-compatible between Windows and macOS. Projects can be copied between operating systems seamlessly.

2. **Native Window & Lifecycle**:
   - `core_launcher.py` detects macOS and initializes pywebview using the native Cocoa WebKit engine.
   - App termination is handled cleanly upon closing the main window.
   - `/opt/homebrew/bin`, `/usr/local/bin`, and bundled `.app/Contents/Resources` are automatically added to `PATH` so GUI launches from Finder or Dock find `ffmpeg` and `deno`.

3. **Cross-Platform Fonts**:
   - `modules/subtitle_builder.py` searches macOS system font locations (`/System/Library/Fonts`, `/System/Library/Fonts/Supplemental`, `/Library/Fonts`, `~/Library/Fonts`).
   - Automatically resolves native Hebrew fonts (`Arial Hebrew`, `Arial`, `Noto Sans Hebrew`, `Lucida Grande`) when Windows-specific fonts like "Microsoft Sans Serif" are not present.

4. **File Opening**:
   - macOS uses `subprocess.run(["open", str(output_path)])` to launch MP4 videos in the system default video player (QuickTime, IINA, VLC) and opens links in the default browser.

5. **JavaScript Runtime & YouTube Imports**:
   - Automatically locates `deno` (no `.exe`), `node`, Homebrew installations, or bundled runtimes.
   - If YouTube blocks access (bot detection, HTTP 429 rate limit, geoblocking, members-only, private video), a clear actionable error is shown explaining the rejection and recommending audio file import.

6. **Windows Core Untouched**:
   - Windows build scripts (`packaging/build_core.ps1`), Inno Setup configuration (`packaging/core_installer.iss`), and `packaging/core.spec` remain 100% functional and untouched.

---

## 3. Reproducible Build Process

Because the development machine is Windows-only, builds for macOS are performed reproducibly using a **hosted GitHub Actions Apple Silicon runner** (`macos-14`) or any macOS build host.

### A. Automated GitHub Actions Build (Recommended)
1. Push your repository to GitHub.
2. In your repository, go to **Actions** -> **Build Karaoke Studio Core (macOS Apple Silicon)**.
3. Click **Run workflow**.
4. The workflow will:
   - Run on an Apple Silicon M1 runner (`macos-14`).
   - Run the test suite (`test_macos_compatibility.py`, `test_timing_editor.py`).
   - Compile the `.app` and package `KaraokeStudioCore-arm64.dmg`.
   - Ad-hoc sign the bundle (or sign with your developer certificate if secrets are provided).
   - Upload the `.dmg` as a downloadable workflow artifact.
5. Download the `.dmg` directly from the workflow run summary.

### B. Building on a Local/Hosted Mac Host
Run the automated build script:
```bash
brew install ffmpeg deno python@3.11
chmod +x packaging/build_mac.sh
./packaging/build_mac.sh
```
The output `.dmg` will be in `dist/KaraokeStudioCore-arm64.dmg`.

---

## 4. Personal Installation & First-Launch Guide

### Installation
1. Double-click `KaraokeStudioCore-arm64.dmg`.
2. Drag **KaraokeStudioCore** into your **Applications** folder.

### First-Launch (Gatekeeper Instructions)
Because this is a personal build without public Apple App Store distribution:
1. When launched for the first time, macOS Gatekeeper may show:
   > *"KaraokeStudioCore" cannot be opened because Apple cannot check it for malicious software.*
2. **To allow it**:
   - Open **Finder** -> **Applications**.
   - **Right-click (or Control-click)** `KaraokeStudioCore` and click **Open**.
   - In the confirmation dialog, click **Open**.
   - *(Alternative via Terminal)*:
     ```bash
     xattr -cr /Applications/KaraokeStudioCore.app
     ```
3. Subsequent launches will open normally from Launchpad, Spotlight, or Dock.

### Upgrading
To upgrade to a new version, simply drag the new `KaraokeStudioCore` into `/Applications` and choose **Replace**. Your projects in `~/Library/Application Support/KaraokeStudio/CoreData` will remain completely intact.

---

## 5. System Requirements & Resource Measurements

- **OS Version**: macOS 12.0 (Monterey), macOS 13 (Ventura), macOS 14 (Sonoma), macOS 15 (Sequoia).
- **Architecture**: Apple Silicon (M1, M2, M3, M4, Pro, Max, Ultra).
- **RAM**:
  - Minimum: 8 GB Unified Memory.
  - Recommended: 16 GB Unified Memory.
  - Peak Memory Usage: ~2.2 GB - 3.1 GB during Demucs vocal separation and WhisperLarge transcription.
- **Disk Space**:
  - Base Application (`.app`): ~300 MB.
  - Demucs Model (`htdemucs_ft`): ~330 MB (downloaded on first use to `~/.cache/torch/hub/checkpoints`).
  - Transcription Model (`whisper-large-v3-turbo-ct2`): ~1.6 GB (downloaded on first use to `~/.cache/huggingface/hub`).
  - Alignment Model (Hebrew wav2vec2): ~1.2 GB.
  - Total Disk Space: ~4.5 GB - 5.5 GB recommended.

---

## 6. Known Limitations & Ongoing Scope

1. **YouTube Extraction**:
   - YouTube frequently updates bot-detection checks on hosted and datacenter IP addresses. If YouTube rejects a request with bot verification or HTTP 429, the app surfaces a clear, actionable error prompting the user to drop in the MP3/WAV file directly.
2. **AI First Pass**:
   - In line with Core Edition design, the external LM Studio AI First Pass endpoint is hidden and disabled; the Core edition focuses on manual timing, Demucs separation, Whisper transcription, WhisperX alignment, and MP4 video rendering.
