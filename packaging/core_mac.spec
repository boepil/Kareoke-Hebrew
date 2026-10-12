# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Karaoke Studio Core on macOS (Apple Silicon arm64 & x86_64).

import os
import shutil
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

PROJECT_ROOT = Path.cwd().resolve()

# Locate FFmpeg binary for bundling (if available)
ffmpeg_path = os.environ.get("KARAOKE_FFMPEG_PATH", "").strip() or shutil.which("ffmpeg")
if not ffmpeg_path:
    for brew_candidate in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if Path(brew_candidate).is_file():
            ffmpeg_path = brew_candidate
            break

# Locate Deno binary for bundling (if available)
deno_path = os.environ.get("KARAOKE_DENO_PATH", "").strip() or shutil.which("deno")
if not deno_path:
    for brew_candidate in ("/opt/homebrew/bin/deno", "/usr/local/bin/deno"):
        if Path(brew_candidate).is_file():
            deno_path = brew_candidate
            break

datas = [
    (str(PROJECT_ROOT / "config.yaml"), "."),
    (str(PROJECT_ROOT / "ui" / "timing_editor.html"), "ui"),
    (str(PROJECT_ROOT / "beep.mp3"), "."),
    (str(PROJECT_ROOT / "commision.mp3"), "."),
]

binaries = []
if ffmpeg_path and Path(ffmpeg_path).is_file():
    binaries.append((str(ffmpeg_path), "."))
if deno_path and Path(deno_path).is_file():
    binaries.append((str(deno_path), "."))

hiddenimports = [
    "modules.audio_extractor",
    "modules.editor_project",
    "modules.lyrics_source",
    "modules.renderer",
    "modules.separator",
    "modules.subtitle_builder",
    "modules.transcriber",
    "modules.aligner",
    "demucs.separate",
    "faster_whisper",
    "whisperx",
    "webview",
    "webview.platforms.cocoa",
    "objc",
    "Foundation",
    "AppKit",
    "WebKit",
]

for package_name in ("demucs", "faster_whisper", "whisper", "whisperx", "pywebview"):
    try:
        pkg_datas, pkg_binaries, pkg_hiddenimports = collect_all(package_name)
    except Exception:
        pkg_datas, pkg_binaries, pkg_hiddenimports = [], [], []
    datas.extend(pkg_datas)
    binaries.extend(pkg_binaries)
    hiddenimports.extend(pkg_hiddenimports)

hiddenimports.extend(collect_submodules("modules"))

a = Analysis(
    [str(PROJECT_ROOT / "packaging" / "core_launcher.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    name="KaraokeStudioCore",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX is disabled on macOS to ensure codesign passes
    console=False,
    exclude_binaries=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="KaraokeStudioCore",
)

app = BUNDLE(
    coll,
    name="KaraokeStudioCore.app",
    icon=None,
    bundle_identifier="com.karaokestudio.core",
    info_plist={
        "CFBundleName": "KaraokeStudioCore",
        "CFBundleDisplayName": "Karaoke Studio Core",
        "CFBundleIdentifier": "com.karaokestudio.core",
        "CFBundleVersion": "1.0.0",
        "CFBundleShortVersionString": "1.0.0",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "12.0",
        "NSRequiresAquaSystemAppearance": False,
        "NSHumanReadableCopyright": "Copyright © 2026 Karaoke Studio",
    },
)
