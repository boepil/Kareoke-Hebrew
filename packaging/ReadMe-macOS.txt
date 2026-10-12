KARAOKE STUDIO CORE (macOS Apple Silicon)
Installation and first-use guide
================================

PACKAGE CONTENTS
----------------
The complete personal installation package is the disk image ending in `.dmg`:

KaraokeStudioCore-arm64.dmg

Copy that single `.dmg` file to your Mac (or download it directly from GitHub Actions). It includes Karaoke Studio Core, its Python runtime, and FFmpeg/runtime helpers. You do not need to install Python, FFmpeg, Demucs, Whisper, WhisperX, or Edge WebView2 separately.

REQUIREMENTS
------------
- Apple Silicon Mac (M1, M2, M3, M4, Pro, Max, Ultra).
- macOS 12.0 (Monterey), macOS 13 (Ventura), macOS 14 (Sonoma), or macOS 15 (Sequoia).
- Internet access for importing online audio and downloading AI model files when first using related audio-processing features.
- At least 5 GB of free disk space for AI model weights and working stems.
- Built-in macOS WebKit is used natively; no third-party browser runtimes or Edge WebView2 are required.

INSTALLATION
------------
1. Double-click KaraokeStudioCore-arm64.dmg to mount the disk image.
2. Drag KaraokeStudioCore into the Applications folder shortcut.
3. First-Launch (Gatekeeper):
   Because this is a personal, ad-hoc signed build, macOS Gatekeeper may show a warning:
   "KaraokeStudioCore cannot be opened because Apple cannot check it for malicious software."
   To open:
   - Go to Finder > Applications.
   - Right-click (Control-click) KaraokeStudioCore and select "Open".
   - In the confirmation prompt, click "Open".
   (Alternative via Terminal: run `xattr -cr /Applications/KaraokeStudioCore.app`)
4. Subsequent launches open normally from Launchpad, Spotlight, or Dock.

FIRST USE AND MODEL DOWNLOADS
-----------------------------
AI model weights are not included in the application bundle to keep download sizes small. The first time you run stem separation or transcription/alignment, the required model weights will download automatically into standard system cache directories:
- Demucs vocal separation: ~/.cache/torch/hub/checkpoints/ (~330 MB)
- Whisper transcription: ~/.cache/huggingface/hub/ (~1.6 GB)
- WhisperX alignment: ~/.cache/huggingface/hub/ (~1.2 GB)

This requires an active internet connection on first use. Once downloaded, all models run locally offline.

PROJECTS AND BACKUPS
--------------------
Projects, manual timings, lyrics, and working files are stored safely outside the app bundle at:

~/Library/Application Support/KaraokeStudio/CoreData

Upgrading to a newer version by replacing the app in /Applications will NOT delete your projects, manual timings, or downloaded models.

Projects are 100% compatible with Windows Core: you can copy project folders between Windows (%LOCALAPPDATA%\KaraokeStudio\CoreData) and macOS (~/Library/Application Support/KaraokeStudio/CoreData) seamlessly.

DEVELOPMENT PREVIEW
-------------------
For continued development on Mac, run the local server without rebuilding the app:

    python3 timing_editor.py

Then open http://127.0.0.1:8765 in Safari or Chrome. Refresh after UI changes; restart the command after Python changes.

TROUBLESHOOTING AND UNINSTALL
-----------------------------
- Gatekeeper blocks opening: Right-click > Open, or run `xattr -cr /Applications/KaraokeStudioCore.app`.
- YouTube import fails: If YouTube prompts for bot verification or rate-limits requests, import the downloaded audio file (.mp3 / .wav) directly into the project.
- Model downloads stall: Check internet connection and verify free disk space.
- To uninstall, drag KaraokeStudioCore from /Applications to Trash. Your project files in ~/Library/Application Support/KaraokeStudio/CoreData will remain safe unless you delete them manually.
