# PyInstaller one-folder build for Karaoke Studio Core.

from pathlib import Path
import os
import shutil

from PyInstaller.utils.hooks import collect_all, collect_submodules


PROJECT_ROOT = Path.cwd().resolve()
ffmpeg_path = os.environ.get("KARAOKE_FFMPEG_PATH", "").strip() or shutil.which("ffmpeg")
if not ffmpeg_path:
    raise SystemExit(
        "FFmpeg was not found. Set KARAOKE_FFMPEG_PATH to ffmpeg.exe or add FFmpeg to PATH."
    )

datas = [
    (str(PROJECT_ROOT / "config.yaml"), "."),
    (str(PROJECT_ROOT / "ui" / "timing_editor.html"), "ui"),
    (str(PROJECT_ROOT / "beep.mp3"), "."),
    (str(PROJECT_ROOT / "commision.mp3"), "."),
]
binaries = [(str(ffmpeg_path), ".")]
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
]

for package_name in ("demucs", "faster_whisper", "whisper", "whisperx"):
    try:
        package_datas, package_binaries, package_hiddenimports = collect_all(package_name)
    except Exception:
        package_datas, package_binaries, package_hiddenimports = [], [], []
    datas.extend(package_datas)
    binaries.extend(package_binaries)
    hiddenimports.extend(package_hiddenimports)

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
    upx=True,
    console=False,
    exclude_binaries=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    name="KaraokeStudioCore",
)
