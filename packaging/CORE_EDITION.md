# Karaoke Studio Core packaging

The Core edition keeps the existing local HTML editor and packages it inside a
native WebView2 window. It supports YouTube/audio import, FFmpeg conversion,
Demucs stem separation, manual timing, and MP4 export. The LM Studio AI First
Pass is hidden in the UI and rejected by the server.

The installer does not include model weights. Demucs and transcription models
are downloaded into the user's normal model cache on first use. Project data is
written to `%LOCALAPPDATA%/KaraokeStudio/CoreData`, not the installation folder.

## Build

From PowerShell on Windows:

```powershell
python -m pip install -r packaging/requirements-core-build.txt
.\packaging\build_core.ps1
```

The build requires FFmpeg. Set `KARAOKE_FFMPEG_PATH` if it is not on `PATH`.
PyInstaller creates `dist/KaraokeStudioCore/`. If Inno Setup is installed, the
script also creates `dist/installer/KaraokeStudioCoreSetup.exe`.
