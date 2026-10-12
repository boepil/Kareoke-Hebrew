from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

launcher_path = Path(__file__).resolve().parent.parent / "packaging" / "core_launcher.py"
spec = importlib.util.spec_from_file_location("core_launcher", launcher_path)
core_launcher = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(core_launcher)
default_user_data_dir = core_launcher.default_user_data_dir

from timing_editor import (
    _format_youtube_error,
    _youtube_runtime_options,
    create_app,
    _editor_paths,
    _project_paths,
    _save_project_lyrics,
    _read_manifest,
    _read_overrides,
    _state_payload,
)
from modules.subtitle_builder import _find_font_path, _load_measurement_font


def test_default_user_data_dir_darwin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("KARAOKE_USER_DATA_DIR", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    expected = tmp_path / "Library" / "Application Support" / "KaraokeStudio" / "CoreData"
    assert default_user_data_dir() == expected.resolve()


def test_default_user_data_dir_honors_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    custom_dir = tmp_path / "custom_data"
    monkeypatch.setenv("KARAOKE_USER_DATA_DIR", str(custom_dir))

    assert default_user_data_dir() == custom_dir.resolve()


def test_youtube_error_formatting_actionable() -> None:
    bot_err = Exception("Sign in to confirm you're not a bot. This helps protect our community.")
    formatted = _format_youtube_error(bot_err)
    assert "bot detection" in formatted.lower()
    assert ".mp3" in formatted

    rate_err = Exception("HTTP Error 429: Too Many Requests")
    formatted_rate = _format_youtube_error(rate_err)
    assert "rate-limiting" in formatted_rate.lower()

    private_err = Exception("Private video. Sign in if you've been granted access.")
    formatted_private = _format_youtube_error(private_err)
    assert "private" in formatted_private.lower()

    geo_err = Exception("The uploader has not made this video available in your country")
    formatted_geo = _format_youtube_error(geo_err)
    assert "geo-restricted" in formatted_geo.lower()

    generic_err = Exception("Connection timeout")
    assert _format_youtube_error(generic_err) == "Connection timeout"


def test_youtube_runtime_options_finds_macos_deno(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_app_root = tmp_path / "AppRoot"
    fake_app_root.mkdir()
    fake_deno = fake_app_root / ("deno.exe" if sys.platform == "win32" else "deno")
    fake_deno.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    monkeypatch.setenv("KARAOKE_APP_ROOT", str(fake_app_root))
    monkeypatch.delenv("KARAOKE_DENO_PATH", raising=False)

    opts = _youtube_runtime_options()
    assert "js_runtimes" in opts
    assert "deno" in opts["js_runtimes"]
    assert opts["js_runtimes"]["deno"]["path"] == str(fake_deno.resolve())


def test_cross_platform_project_format_compatibility(tmp_path: Path) -> None:
    # Simulate project created on Windows with manual timings and Hebrew lyrics
    temp_dir = tmp_path / "temp"
    (temp_dir / "subtitles").mkdir(parents=True)
    (temp_dir / "state").mkdir(parents=True)
    (temp_dir / "audio").mkdir(parents=True)
    (temp_dir / "projects" / "song1" / "subtitles").mkdir(parents=True)
    (temp_dir / "projects" / "song1" / "state").mkdir(parents=True)
    (temp_dir / "projects" / "song1" / "audio").mkdir(parents=True)

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
paths:
  input_dir: {tmp_path / "input"}
  output_dir: {tmp_path / "output"}
  temp_dir: {temp_dir}
subtitle_builder:
  manifest_name: subtitles_manifest.json
  timing_overrides_name: timing_overrides.json
  font_name: Microsoft Sans Serif
""",
        encoding="utf-8",
    )

    base_paths = _editor_paths(config_path)
    proj_paths = _project_paths(base_paths, "song1")

    # Write initial project state
    state = {
        "project_id": "song1",
        "project_name": "שלום עליכם",
        "lyrics_text": "שלום עליכם מלאכי השרת\nמלאכי עליון",
        "source_name": "song.mp3",
    }
    proj_paths.state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    # Save lyrics and ensure manifest is generated
    _save_project_lyrics(proj_paths, state["lyrics_text"])

    manifest = _read_manifest(proj_paths)
    assert len(manifest["lines"]) == 2
    assert manifest["lines"][0]["text"] == "שלום עליכם מלאכי השרת"
    assert manifest["lines"][1]["text"] == "מלאכי עליון"

    # Add manual timing overrides
    overrides = {
        "placed_word_count": 5,
        "overrides": {
            "segment_000": {"start": 1.0, "end": 4.5, "words": [{"word": "שלום", "start": 1.0, "end": 1.8}]}
        },
    }
    proj_paths.overrides_path.write_text(json.dumps(overrides, ensure_ascii=False), encoding="utf-8")

    # Read back and verify complete compatibility
    loaded_overrides = _read_overrides(proj_paths)
    assert loaded_overrides["placed_word_count"] == 5
    assert "segment_000" in loaded_overrides["overrides"]
    assert loaded_overrides["overrides"]["segment_000"]["words"][0]["word"] == "שלום"


def test_font_discovery_and_fallback(tmp_path: Path) -> None:
    # Font path resolution with nonexistent font falls back gracefully
    font = _load_measurement_font("NonExistentFontName123", "", 24)
    assert font is not None

    # Direct candidate check
    fake_font = tmp_path / "CustomHebrew.ttf"
    fake_font.write_bytes(b"dummy font data")
    resolved = _find_font_path("CustomHebrew.ttf", str(fake_font))
    assert resolved == str(fake_font)
