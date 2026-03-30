"""Flask-backed timing editor for manual Hebrew karaoke subtitle correction."""

from __future__ import annotations

import argparse
import audioop
import json
import os
import re
import threading
import time
import wave
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from flask import Flask, Response, jsonify, request, send_file

from main import load_config
from modules.audio_extractor import extract_and_normalize_audio
from modules.renderer import render_video
from modules.separator import separate_vocals
from modules.subtitle_builder import (
    _ass_placeholder_line,
    _build_image_events,
    build_ass_dialogue_lines,
    build_ass_header,
    build_subtitle_settings,
)


@dataclass(frozen=True)
class EditorPaths:
    config_path: Path
    root_dir: Path
    input_dir: Path
    temp_dir: Path
    manifest_path: Path
    editor_manifest_path: Path
    overrides_path: Path
    state_path: Path
    audio_path: Path
    ui_path: Path


LEGACY_PROJECT_KEY = "__legacy__"


def _resolve_config_path(config_path: str | Path) -> Path:
    return Path(config_path).expanduser().resolve()


def _resolve_from_config(config_dir: Path, raw_path: str | Path) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return (config_dir / path).resolve()


def _editor_paths(config_path: str | Path) -> EditorPaths:
    resolved_config = _resolve_config_path(config_path)
    config = load_config(resolved_config)
    paths = config.get("paths")
    subtitle_builder = config.get("subtitle_builder")
    if not isinstance(paths, Mapping):
        raise KeyError("Missing 'paths' section in config")
    if not isinstance(subtitle_builder, Mapping):
        raise KeyError("Missing 'subtitle_builder' section in config")

    config_dir = resolved_config.parent
    input_dir = _resolve_from_config(config_dir, str(paths["input_dir"]))
    temp_dir = _resolve_from_config(config_dir, str(paths["temp_dir"]))
    manifest_name = str(subtitle_builder.get("manifest_name", "subtitles_manifest.json"))
    overrides_name = str(subtitle_builder.get("timing_overrides_name", "timing_overrides.json"))
    audio_path = temp_dir / "audio.wav"
    ui_path = (config_dir / "ui" / "timing_editor.html").resolve()
    return EditorPaths(
        config_path=resolved_config,
        root_dir=config_dir,
        input_dir=input_dir,
        temp_dir=temp_dir,
        manifest_path=temp_dir / manifest_name,
        editor_manifest_path=temp_dir / "timing_editor_manifest.json",
        overrides_path=temp_dir / overrides_name,
        state_path=temp_dir / "state.json",
        audio_path=audio_path,
        ui_path=ui_path,
    )


def _projects_root(base_paths: EditorPaths) -> Path:
    return base_paths.temp_dir / "projects"


def _current_project_marker(base_paths: EditorPaths) -> Path:
    return base_paths.temp_dir / "current_project.txt"


def _project_storage_key(name: str, fallback: str = "project") -> str:
    cleaned = _sanitize_filename(name, fallback)
    return cleaned.replace(".", "_")


def _project_paths(base_paths: EditorPaths, project_key: str) -> EditorPaths:
    if not project_key or project_key == LEGACY_PROJECT_KEY:
        return base_paths

    temp_dir = _projects_root(base_paths) / project_key
    return EditorPaths(
        config_path=base_paths.config_path,
        root_dir=base_paths.root_dir,
        input_dir=base_paths.input_dir,
        temp_dir=temp_dir,
        manifest_path=temp_dir / base_paths.manifest_path.name,
        editor_manifest_path=temp_dir / base_paths.editor_manifest_path.name,
        overrides_path=temp_dir / base_paths.overrides_path.name,
        state_path=temp_dir / base_paths.state_path.name,
        audio_path=temp_dir / base_paths.audio_path.name,
        ui_path=base_paths.ui_path,
    )


def _legacy_project_exists(base_paths: EditorPaths) -> bool:
    return any(
        path.exists()
        for path in (
            base_paths.state_path,
            base_paths.editor_manifest_path,
            base_paths.manifest_path,
            base_paths.overrides_path,
            base_paths.audio_path,
        )
    )


def _active_project_key(base_paths: EditorPaths) -> str:
    marker = _current_project_marker(base_paths)
    if marker.exists():
        raw = marker.read_text(encoding="utf-8").strip()
        if raw:
            return raw
    if _legacy_project_exists(base_paths):
        return LEGACY_PROJECT_KEY
    return ""


def _set_active_project_key(base_paths: EditorPaths, project_key: str) -> None:
    marker = _current_project_marker(base_paths)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(project_key, encoding="utf-8")


def _json_response(payload: Mapping[str, Any], status: int = 200) -> Response:
    return Response(
        json.dumps(payload, ensure_ascii=False, indent=2),
        status=status,
        mimetype="application/json",
    )


def _load_json_file(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        loaded = json.load(handle)
    if not isinstance(loaded, dict):
        raise ValueError(f"JSON file must contain an object: {path}")
    return loaded


def _default_overrides() -> dict[str, Any]:
    return {
        "version": 1,
        "exported_at": None,
        "global_offset": 0.0,
        "placed_word_count": 0,
        "lyrics_text": "",
        "lines": {},
        "words": {},
    }


def _sanitize_override_body(raw: Mapping[str, Any]) -> dict[str, Any]:
    def clean_entries(raw_entries: Any) -> dict[str, dict[str, float]]:
        if not isinstance(raw_entries, Mapping):
            raw_entries = {}
        cleaned_entries: dict[str, dict[str, float]] = {}
        for entry_id, override in raw_entries.items():
            if not isinstance(override, Mapping):
                raise ValueError(f"Override for '{entry_id}' must be an object")
            cleaned: dict[str, float] = {}
            for key in ("start", "end", "offset", "stretch"):
                value = override.get(key)
                if value is None:
                    continue
                cleaned[key] = float(value)
            cleaned_entries[str(entry_id)] = cleaned
        return cleaned_entries

    cleaned_lines = clean_entries(raw.get("lines"))
    cleaned_words = clean_entries(raw.get("words"))
    return {
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "global_offset": float(raw.get("global_offset", 0.0)),
        "placed_word_count": max(int(raw.get("placed_word_count", 0)), 0),
        "lyrics_text": str(raw.get("lyrics_text", "")).strip(),
        "lines": cleaned_lines,
        "words": cleaned_words,
    }


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def _sanitize_filename(name: str, fallback: str) -> str:
    cleaned = Path(name).name.strip()
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or fallback


def _clean_text_lines(raw_text: str) -> list[str]:
    lines = [re.sub(r"\s+", " ", line).strip() for line in raw_text.splitlines()]
    return [line for line in lines if line]


def _split_words(text: str) -> list[str]:
    return [token for token in re.split(r"\s+", text.strip()) if token]


def _read_wav_duration(audio_path: Path) -> float:
    with wave.open(str(audio_path), "rb") as wav_file:
        frame_rate = wav_file.getframerate()
        total_frames = wav_file.getnframes()
    if frame_rate <= 0:
        return 0.0
    return round(total_frames / frame_rate, 6)


def _build_word_stream(lines: list[str], duration_seconds: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    flattened = [(line_index, token) for line_index, line in enumerate(lines) for token in _split_words(line)]
    if not flattened:
        return [], []

    total_duration = max(duration_seconds, float(len(flattened)))
    slot_duration = total_duration / len(flattened)
    words: list[dict[str, Any]] = []
    line_to_word_ids: dict[int, list[str]] = {index: [] for index in range(len(lines))}

    for index, (line_index, token) in enumerate(flattened):
        start = round(index * slot_duration, 3)
        end = round((index + 1) * slot_duration, 3)
        if end <= start:
            end = round(start + 0.01, 3)
        word_id = f"word_{index:04d}"
        line_to_word_ids[line_index].append(word_id)
        words.append(
            {
                "id": word_id,
                "index": index,
                "line_index": line_index,
                "text": token,
                "start": start,
                "end": end,
            }
        )

    lines_payload: list[dict[str, Any]] = []
    word_lookup = {entry["id"]: entry for entry in words}
    for line_index, text in enumerate(lines):
        word_ids = line_to_word_ids.get(line_index, [])
        if word_ids:
            start = float(word_lookup[word_ids[0]]["start"])
            end = float(word_lookup[word_ids[-1]]["end"])
        else:
            start = 0.0
            end = 0.01
        lines_payload.append(
            {
                "id": f"line_{line_index:03d}",
                "index": line_index,
                "text": text,
                "start": start,
                "end": end,
                "word_ids": word_ids,
            }
        )
    return words, lines_payload


def _default_manual_overrides() -> dict[str, Any]:
    return _default_overrides()


def _empty_manifest() -> dict[str, Any]:
    return {"lines": [], "words": []}


def _lyrics_path(paths: EditorPaths) -> Path:
    return paths.temp_dir / "lyrics.txt"


def _state_payload(paths: EditorPaths) -> dict[str, Any]:
    if not paths.state_path.exists():
        return {}
    loaded = _load_json_file(paths.state_path)
    return loaded if isinstance(loaded, dict) else {}


def _sync_manifest_text_with_lyrics(paths: EditorPaths, lyrics_text: str) -> None:
    if not paths.editor_manifest_path.exists():
        return

    manifest = _read_manifest(paths)
    cleaned_lines = _clean_text_lines(lyrics_text)
    flattened_words = [token for line in cleaned_lines for token in _split_words(line)]
    existing_words = manifest.get("words", [])
    if not isinstance(existing_words, list) or len(flattened_words) != len(existing_words):
        return

    for index, word in enumerate(existing_words):
        if isinstance(word, dict):
            word["text"] = flattened_words[index]

    words_by_id = {
        str(word.get("id", "")): str(word.get("text", "")).strip()
        for word in existing_words
        if isinstance(word, dict)
    }

    for line in manifest.get("lines", []):
        if not isinstance(line, dict):
            continue
        word_ids = [str(word_id) for word_id in line.get("word_ids", [])]
        if word_ids:
            line["text"] = " ".join(words_by_id.get(word_id, "").strip() for word_id in word_ids).strip()

    _write_json_atomic(paths.editor_manifest_path, manifest)


def _save_project_lyrics(paths: EditorPaths, lyrics_text: str) -> None:
    cleaned_text = "\n".join(_clean_text_lines(lyrics_text))
    _lyrics_path(paths).write_text(cleaned_text, encoding="utf-8")

    state = _state_payload(paths)
    if state:
        state["lyrics_text"] = cleaned_text
        state["line_count"] = len(_clean_text_lines(cleaned_text))
        state["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        _write_json_atomic(paths.state_path, state)

    _sync_manifest_text_with_lyrics(paths, cleaned_text)


def _project_lyrics_text(paths: EditorPaths) -> str:
    state = _state_payload(paths)
    state_text = str(state.get("lyrics_text", "")).strip()
    if state_text:
        return state_text

    lyrics_file = _lyrics_path(paths)
    if lyrics_file.exists():
        try:
            return lyrics_file.read_text(encoding="utf-8").strip()
        except Exception:
            pass

    if paths.editor_manifest_path.exists() or paths.manifest_path.exists():
        try:
            manifest = _read_manifest(paths)
            raw_lines = manifest.get("lines", [])
            if isinstance(raw_lines, list):
                derived_lines = [
                    str(item.get("text", "")).strip()
                    for item in raw_lines
                    if isinstance(item, Mapping) and str(item.get("text", "")).strip()
                ]
                if derived_lines:
                    return "\n".join(derived_lines)
        except Exception:
            pass

    return ""


def _project_display_name(paths: EditorPaths, project_key: str) -> str:
    state = _state_payload(paths)
    project_name = str(state.get("project_name", "")).strip()
    if project_name:
        return project_name
    source_name = _guess_input_audio_name(paths, state)
    if source_name:
        return Path(source_name).stem
    if project_key == LEGACY_PROJECT_KEY:
        return "Legacy Project"
    return project_key


def _project_summary(base_paths: EditorPaths, project_key: str) -> dict[str, Any]:
    project_paths = _project_paths(base_paths, project_key)
    state = _state_payload(project_paths)
    return {
        "id": project_key,
        "name": _project_display_name(project_paths, project_key),
        "word_count": int(state.get("word_count", 0) or 0),
        "line_count": int(state.get("line_count", 0) or 0),
        "updated_at": str(state.get("updated_at", "")).strip(),
        "source_name": _guess_input_audio_name(project_paths, state),
        "is_active": project_key == _active_project_key(base_paths),
    }


def _list_projects(base_paths: EditorPaths) -> list[dict[str, Any]]:
    projects: list[dict[str, Any]] = []
    if _legacy_project_exists(base_paths):
        projects.append(_project_summary(base_paths, LEGACY_PROJECT_KEY))

    projects_root = _projects_root(base_paths)
    if projects_root.exists():
        for project_dir in sorted(path for path in projects_root.iterdir() if path.is_dir()):
            projects.append(_project_summary(base_paths, project_dir.name))
    return projects


def _guess_input_audio_name(paths: EditorPaths, state: Mapping[str, Any]) -> str:
    source_name = str(state.get("source_name", "")).strip()
    if source_name:
        return source_name

    original_audio_name = str(state.get("original_audio_name", "")).strip()
    if original_audio_name:
        return original_audio_name

    audio_source_raw = str(state.get("audio_source", "")).strip()
    audio_source_name = Path(audio_source_raw).name.strip() if audio_source_raw else ""
    input_files = sorted(path for path in paths.input_dir.glob("*.mp3") if path.is_file())

    if len(input_files) == 1:
        only_name = input_files[0].name.strip()
        if not audio_source_name or audio_source_name.lower().startswith("youtube_"):
            return only_name

    if audio_source_name:
        return audio_source_name

    return ""


def _write_manual_project(
    paths: EditorPaths,
    audio_source: Path,
    lyrics_text: str,
    config_path: Path,
    original_name: str,
    project_name: str,
) -> dict[str, Any]:
    paths.input_dir.mkdir(parents=True, exist_ok=True)
    paths.temp_dir.mkdir(parents=True, exist_ok=True)

    stored_input_name = _sanitize_filename(original_name, "manual_input.mp3")
    stored_input_path = paths.input_dir / stored_input_name
    stored_input_path.write_bytes(audio_source.read_bytes())

    extract_and_normalize_audio(stored_input_path, paths.audio_path, config_path)
    duration_seconds = _read_wav_duration(paths.audio_path)
    lyric_lines = _clean_text_lines(lyrics_text)
    words, line_entries = _build_word_stream(lyric_lines, duration_seconds)

    manifest = {
        "version": 1,
        "source_audio": str(stored_input_path),
        "lines": line_entries,
        "words": words,
    }
    overrides = _default_manual_overrides()
    state = {
        "mode": "manual",
        "status": "ready",
        "project_name": project_name,
        "audio_source": str(stored_input_path),
        "source_name": stored_input_name,
        "original_audio_name": str(original_name).strip(),
        "lyrics_text": "\n".join(lyric_lines),
        "audio_wav": str(paths.audio_path),
        "manifest_path": str(paths.editor_manifest_path),
        "overrides_path": str(paths.overrides_path),
        "line_count": len(lyric_lines),
        "word_count": len(words),
        "duration_seconds": duration_seconds,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }

    state["artifacts"] = {
        "audio_wav": str(paths.audio_path),
    }

    _write_json_atomic(paths.editor_manifest_path, manifest)
    _write_json_atomic(paths.overrides_path, overrides)
    _write_json_atomic(paths.state_path, state)
    _lyrics_path(paths).write_text("\n".join(lyric_lines), encoding="utf-8")
    return state


def _write_empty_project(paths: EditorPaths, project_name: str, lyrics_text: str = "") -> dict[str, Any]:
    paths.temp_dir.mkdir(parents=True, exist_ok=True)
    lyric_lines = _clean_text_lines(lyrics_text)
    seed_duration = float(max(sum(len(_split_words(line)) for line in lyric_lines), 0))
    words, lines_payload = _build_word_stream(lyric_lines, seed_duration)
    manifest = {"lines": lines_payload, "words": words}
    overrides = _default_manual_overrides()
    overrides["lyrics_text"] = "\n".join(lyric_lines)
    state = {
        "version": 1,
        "mode": "manual",
        "status": "empty",
        "project_name": project_name,
        "audio_source": "",
        "original_audio_name": "",
        "source_name": "",
        "line_count": len(lines_payload),
        "word_count": len(words),
        "lyrics_text": "\n".join(lyric_lines),
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "artifacts": {},
    }
    _write_json_atomic(paths.editor_manifest_path, manifest)
    _write_json_atomic(paths.overrides_path, overrides)
    _write_json_atomic(paths.state_path, state)
    _lyrics_path(paths).write_text("\n".join(lyric_lines), encoding="utf-8")
    return state


def _read_manifest(paths: EditorPaths) -> dict[str, Any]:
    manifest_source = paths.editor_manifest_path if paths.editor_manifest_path.exists() else paths.manifest_path
    manifest = _load_json_file(manifest_source)
    raw_lines = manifest.get("lines")
    raw_words = manifest.get("words")
    if not isinstance(raw_lines, list):
        raise ValueError(f"Manifest must contain a 'lines' array: {manifest_source}")
    if raw_words is None:
        raw_words = []
    if not isinstance(raw_words, list):
        raise ValueError(f"Manifest must contain a 'words' array: {manifest_source}")

    normalized_lines: list[dict[str, Any]] = []
    for index, item in enumerate(raw_lines):
        if not isinstance(item, Mapping):
            raise ValueError(f"Manifest line at index {index} must be an object")
        normalized_lines.append(
            {
                "id": str(item.get("id", f"line_{index:03d}")),
                "index": index,
                "text": str(item.get("text", "")).strip(),
                "start": float(item.get("start", 0.0)),
                "end": float(item.get("end", 0.0)),
                "word_ids": [str(word_id) for word_id in item.get("word_ids", []) if str(word_id)],
            }
        )

    normalized_words: list[dict[str, Any]] = []
    for index, item in enumerate(raw_words):
        if not isinstance(item, Mapping):
            raise ValueError(f"Manifest word at index {index} must be an object")
        normalized_words.append(
            {
                "id": str(item.get("id", f"word_{index:04d}")),
                "index": int(item.get("index", index)),
                "line_index": int(item.get("line_index", 0)),
                "text": str(item.get("text", "")).strip(),
                "start": float(item.get("start", 0.0)),
                "end": float(item.get("end", 0.0)),
            }
        )
    return {"version": 1, "lines": normalized_lines, "words": normalized_words}


def _read_overrides(paths: EditorPaths) -> dict[str, Any]:
    if not paths.overrides_path.exists():
        return _default_overrides()
    loaded = _load_json_file(paths.overrides_path)
    merged = _default_overrides()
    merged.update(loaded)
    if not isinstance(merged.get("lines"), Mapping):
        merged["lines"] = {}
    if not isinstance(merged.get("words"), Mapping):
        merged["words"] = {}
    return merged


def _resolve_manual_word_windows(
    manifest: Mapping[str, Any],
    overrides: Mapping[str, Any],
) -> dict[str, tuple[float, float, str]]:
    global_offset = float(overrides.get("global_offset", 0.0))
    word_overrides = overrides.get("words", {})
    if not isinstance(word_overrides, Mapping):
        word_overrides = {}

    windows: dict[str, tuple[float, float, str]] = {}
    raw_words = manifest.get("words", [])
    if not isinstance(raw_words, list):
        raw_words = []

    for item in raw_words:
        if not isinstance(item, Mapping):
            continue
        word_id = str(item.get("id", "")).strip()
        if not word_id:
            continue

        start = float(item.get("start", 0.0)) + global_offset
        end = float(item.get("end", start + 0.12)) + global_offset
        override = word_overrides.get(word_id, {})
        if isinstance(override, Mapping):
            if "offset" in override:
                offset = float(override["offset"])
                start += offset
                end += offset
            if override.get("start") is not None:
                start = float(override["start"])
            if override.get("end") is not None:
                end = float(override["end"])
            if "stretch" in override:
                duration = max(end - start, 0.001) * max(float(override["stretch"]), 0.01)
                end = start + duration
        if end <= start:
            end = start + 0.01
        windows[word_id] = (start, end, str(item.get("text", "")).strip())
    return windows


def _build_manual_subtitles(paths: EditorPaths, config: Mapping[str, Any]) -> Path:
    manifest = _read_manifest(paths)
    overrides = _read_overrides(paths)
    settings = build_subtitle_settings(config)
    temp_dir = paths.temp_dir
    temp_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = temp_dir / settings["assets_dir_name"]
    settings["assets_dir"] = str(assets_dir)

    word_windows = _resolve_manual_word_windows(manifest, overrides)
    line_payloads: list[dict[str, Any]] = []

    raw_lines = manifest.get("lines", [])
    if not isinstance(raw_lines, list):
        raw_lines = []

    for index, item in enumerate(raw_lines):
        if not isinstance(item, Mapping):
            continue
        line_id = str(item.get("id", f"line_{index:03d}"))
        word_ids = [str(word_id) for word_id in item.get("word_ids", []) if str(word_id)]
        windows = [word_windows[word_id] for word_id in word_ids if word_id in word_windows]
        if not windows:
            continue

        start = float(windows[0][0])
        end = float(windows[-1][1])
        display_text = str(item.get("text", "")).strip()
        line_payloads.append(
            {
                "id": line_id,
                "text": display_text,
                "start": start,
                "end": end,
                "word_ids": word_ids,
                "windows": windows,
            }
        )

    dialogue_lines: list[str] = []
    image_events: list[dict[str, Any]] = []
    line_entries: list[dict[str, Any]] = []
    overlap_epsilon = 0.01
    for index, payload in enumerate(line_payloads):
        next_start = None
        if index + 1 < len(line_payloads):
            next_start = float(line_payloads[index + 1]["start"])

        start = float(payload["start"])
        end = float(payload["end"])
        if next_start is not None:
            end = min(end, max(start + overlap_epsilon, next_start - overlap_epsilon))

        windows: list[tuple[float, float, str]] = []
        for word_start, word_end, word_text in payload["windows"]:
            clamped_start = float(word_start)
            if clamped_start >= end:
                break
            clamped_end = min(float(word_end), end)
            if clamped_end <= clamped_start:
                clamped_end = min(end, clamped_start + overlap_epsilon)
            if clamped_end <= clamped_start:
                continue
            windows.append((clamped_start, clamped_end, word_text))

        if not windows:
            continue

        display_text = str(payload["text"]).strip()
        dialogue_lines.append(_ass_placeholder_line(start, end, display_text))
        image_events.extend(
            _build_image_events(
                str(payload["id"]),
                display_text,
                start,
                end,
                windows,
                assets_dir,
                settings,
            )
        )
        line_entries.append(
            {
                "id": str(payload["id"]),
                "text": display_text,
                "start": start,
                "end": end,
                "word_ids": list(payload["word_ids"]),
            }
        )

    if not dialogue_lines:
        raise RuntimeError("Manual project has no timed subtitle lines to export")

    ass_path = temp_dir / settings["output_ass_name"]
    manifest_path = temp_dir / settings["manifest_name"]
    ass_path.write_text(build_ass_header(settings) + "\n".join(dialogue_lines) + "\n", encoding="utf-8")
    manifest_path.write_text(
        json.dumps({"lines": line_entries, "events": image_events}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return ass_path


def _resolved_manual_words(
    manifest: Mapping[str, Any],
    overrides: Mapping[str, Any],
) -> list[dict[str, Any]]:
    raw_words = manifest.get("words", [])
    if not isinstance(raw_words, list):
        return []

    word_overrides = overrides.get("words", {})
    if not isinstance(word_overrides, Mapping):
        word_overrides = {}

    resolved_words: list[dict[str, Any]] = []
    for index, item in enumerate(raw_words):
        if not isinstance(item, Mapping):
            continue
        start = float(item.get("start", 0.0))
        end = float(item.get("end", start + 0.12))
        override = word_overrides.get(str(item.get("id", "")), {})
        if isinstance(override, Mapping):
            start = float(override.get("start", start))
            end = float(override.get("end", end))
        if end <= start:
            end = start + 0.12
        resolved_words.append(
            {
                "id": str(item.get("id", f"word_{index:04d}")),
                "index": int(item.get("index", index)),
                "line_index": int(item.get("line_index", 0)),
                "text": str(item.get("text", "")).strip(),
                "start": start,
                "end": end,
            }
        )
    return sorted(resolved_words, key=lambda word: word["index"])


def _manual_segments_from_manifest(
    manifest: Mapping[str, Any],
    overrides: Mapping[str, Any],
) -> list[dict[str, Any]]:
    resolved_words = _resolved_manual_words(manifest, overrides)
    word_lookup = {word["id"]: word for word in resolved_words}
    raw_lines = manifest.get("lines", [])
    if not isinstance(raw_lines, list):
        raw_lines = []

    segments: list[dict[str, Any]] = []
    for line_index, line in enumerate(raw_lines):
        if not isinstance(line, Mapping):
            continue
        word_ids = line.get("word_ids", [])
        if isinstance(word_ids, list) and word_ids:
            line_words = [word_lookup[word_id] for word_id in word_ids if word_id in word_lookup]
        else:
            line_words = [word for word in resolved_words if word["line_index"] == line_index]
        if not line_words:
            continue
        line_words = sorted(line_words, key=lambda word: word["index"])
        segments.append(
            {
                "start": float(line_words[0]["start"]),
                "end": float(line_words[-1]["end"]),
                "text": " ".join(word["text"] for word in line_words).strip(),
                "words": [
                    {
                        "word": word["text"],
                        "start": float(word["start"]),
                        "end": float(word["end"]),
                    }
                    for word in line_words
                ],
            }
        )
    return segments


def _renderer_audio_path(paths: EditorPaths, config: Mapping[str, Any]) -> Path:
    state: dict[str, Any] = {}
    if paths.state_path.exists():
        try:
            state = _load_json_file(paths.state_path)
        except Exception:
            state = {}

    renderer_settings = config.get("renderer")
    artifact_name = "audio_wav"
    if isinstance(renderer_settings, Mapping):
        artifact_name = str(renderer_settings.get("audio_artifact", artifact_name))

    artifacts = state.get("artifacts", {})
    if isinstance(artifacts, Mapping):
        artifact_path = artifacts.get(artifact_name)
        if artifact_path:
          resolved = Path(str(artifact_path))
          if resolved.exists():
              return resolved

    if artifact_name == "audio_wav" and paths.audio_path.exists():
        return paths.audio_path
    fallback = paths.temp_dir / f"{artifact_name.replace('_wav', '')}.wav"
    if fallback.exists():
        return fallback
    raise FileNotFoundError(f"Renderer audio artifact not found: {artifact_name}")


def _manual_render_audio_path(paths: EditorPaths) -> Path:
    state = _state_payload(paths)
    artifacts = state.get("artifacts", {})
    if isinstance(artifacts, Mapping):
        no_vocals = artifacts.get("no_vocals_wav")
        if no_vocals:
            no_vocals_path = Path(str(no_vocals))
            if no_vocals_path.exists():
                return no_vocals_path

    no_vocals_fallback = paths.temp_dir / "no_vocals.wav"
    if no_vocals_fallback.exists():
        return no_vocals_fallback
    return paths.audio_path


def _ensure_project_backing_track(paths: EditorPaths) -> Path:
    preferred_audio = _manual_render_audio_path(paths)
    if preferred_audio.exists() and preferred_audio != paths.audio_path:
        return preferred_audio

    if not paths.audio_path.exists():
        return paths.audio_path

    try:
        separation_outputs = separate_vocals(paths.audio_path, paths.config_path)
    except Exception:
        return paths.audio_path

    state = _state_payload(paths)
    existing_artifacts = state.get("artifacts", {})
    artifacts = dict(existing_artifacts) if isinstance(existing_artifacts, Mapping) else {}
    artifacts["audio_wav"] = str(paths.audio_path)
    artifacts["vocals_wav"] = str(separation_outputs.get("vocals", paths.temp_dir / "vocals.wav"))
    artifacts["no_vocals_wav"] = str(separation_outputs.get("no_vocals", paths.temp_dir / "no_vocals.wav"))
    state["artifacts"] = artifacts
    _write_json_atomic(paths.state_path, state)

    no_vocals_path = Path(artifacts["no_vocals_wav"])
    if no_vocals_path.exists():
        return no_vocals_path
    return paths.audio_path


def _render_manual_project(paths: EditorPaths) -> Path:
    config = load_config(paths.config_path)
    manifest = _read_manifest(paths)
    overrides = _read_overrides(paths)
    segments = _manual_segments_from_manifest(manifest, overrides)
    if not segments:
        raise ValueError("No timed manual segments available to render")

    settings = build_subtitle_settings(config)
    settings["assets_dir"] = str(paths.temp_dir / settings["assets_dir_name"])
    settings["timing_overrides"] = {}

    dialogue_lines, image_events, line_entries = build_ass_dialogue_lines(segments, settings)
    ass_path = paths.temp_dir / settings["output_ass_name"]
    manifest_path = paths.temp_dir / settings["manifest_name"]
    ass_path.write_text(build_ass_header(settings) + "\n".join(dialogue_lines) + "\n", encoding="utf-8")
    manifest_path.write_text(
        json.dumps({"lines": line_entries, "events": image_events}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    audio_path = _manual_render_audio_path(paths)
    return render_video(audio_path, ass_path, config)


def _waveform_max_value(sample_width: int) -> int:
    max_values = {1: 127, 2: 32767, 3: 8388607, 4: 2147483647}
    if sample_width not in max_values:
        raise ValueError(f"Unsupported sample width for waveform extraction: {sample_width}")
    return max_values[sample_width]


@lru_cache(maxsize=8)
def _cached_waveform(path_str: str, mtime_ns: int, bins: int) -> dict[str, Any]:
    path = Path(path_str)
    with wave.open(str(path), "rb") as wav_file:
        frame_rate = wav_file.getframerate()
        sample_width = wav_file.getsampwidth()
        channels = wav_file.getnchannels()
        total_frames = wav_file.getnframes()
        if frame_rate <= 0 or total_frames <= 0:
            return {"duration": 0.0, "sample_rate": frame_rate, "channels": channels, "peaks": []}

        frames_per_bin = max(total_frames // bins, 1)
        max_value = float(_waveform_max_value(sample_width))
        peaks: list[float] = []
        frames_remaining = total_frames

        while frames_remaining > 0:
            chunk_frames = min(frames_per_bin, frames_remaining)
            chunk = wav_file.readframes(chunk_frames)
            if not chunk:
                break
            peak = audioop.max(chunk, sample_width) / max_value
            peaks.append(round(min(max(peak, 0.0), 1.0), 6))
            frames_remaining -= chunk_frames

    return {
        "duration": round(total_frames / frame_rate, 6),
        "sample_rate": frame_rate,
        "channels": channels,
        "peaks": peaks,
    }


def _waveform_payload(audio_path: Path, bins: int) -> dict[str, Any]:
    stat = audio_path.stat()
    payload = _cached_waveform(str(audio_path), stat.st_mtime_ns, bins)
    return {
        "version": 1,
        "audio_file": audio_path.name,
        "bins": bins,
        **payload,
    }


def _audio_duration_seconds(audio_path: Path) -> float:
    with wave.open(str(audio_path), "rb") as handle:
        frame_rate = handle.getframerate()
        if frame_rate <= 0:
            return 0.0
        frame_count = handle.getnframes()
    return max(frame_count / float(frame_rate), 0.0)


def _manual_output_video_name(paths: EditorPaths) -> str:
    state = _state_payload(paths)
    source_name = _guess_input_audio_name(paths, state)
    if source_name:
        base_name = Path(source_name).stem.strip() or "karaoke"
        return f"{base_name} (Kareoke).mp4"

    original_audio_name = str(state.get("original_audio_name", "")).strip()
    if original_audio_name:
        base_name = Path(original_audio_name).stem.strip() or "karaoke"
        return f"{base_name} (Kareoke).mp4"

    audio_source_raw = str(state.get("audio_source", "")).strip()
    if audio_source_raw:
        base_name = Path(audio_source_raw).stem.strip() or "karaoke"
        return f"{base_name} (Kareoke).mp4"

    project_name = str(state.get("project_name", "")).strip()
    if project_name:
        return f"{project_name} (Kareoke).mp4"

    config = load_config(paths.config_path)
    return str(config.get("renderer", {}).get("output_video_name", "karaoke.mp4"))


def _default_output_video_path(paths: EditorPaths) -> Path:
    config = load_config(paths.config_path)
    output_dir = _resolve_from_config(paths.config_path.parent, str(config["paths"]["output_dir"]))
    output_name = _manual_output_video_name(paths)
    return output_dir / output_name


def create_app(config_path: str | Path = "config.yaml") -> Flask:
    base_paths = _editor_paths(config_path)
    app = Flask(__name__)
    app.config["TIMING_EDITOR_PATHS"] = base_paths
    app.config["EXPORT_JOB"] = {
        "status": "idle",
        "detail": None,
        "progress": 0,
        "started_at": None,
        "estimated_total_seconds": None,
        "project_id": None,
        "output_video": None,
        "subtitles_ass": None,
        "error": None,
    }
    app.config["EXPORT_LOCK"] = threading.Lock()

    def current_paths() -> EditorPaths:
        project_key = _active_project_key(base_paths)
        return _project_paths(base_paths, project_key)

    def run_export_job(project_paths: EditorPaths, project_key: str) -> None:
        try:
            config = load_config(project_paths.config_path)
            output_video_name = _manual_output_video_name(project_paths)
            render_config = dict(config)
            renderer_config = dict(render_config.get("renderer", {}))
            renderer_config["output_video_name"] = output_video_name
            renderer_config["audio_artifact"] = "no_vocals_wav"
            render_config["renderer"] = renderer_config
            job_started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            set_export_job(
                status="preparing_audio",
                detail="Preparing backing track",
                progress=5,
                started_at=job_started_at,
                estimated_total_seconds=None,
                project_id=project_key,
                error=None,
            )
            render_audio_path = _ensure_project_backing_track(project_paths)
            set_export_job(
                status="building_subtitles",
                detail="Building subtitles",
                progress=10,
                started_at=job_started_at,
                estimated_total_seconds=None,
                project_id=project_key,
                error=None,
            )
            subtitles_path = _build_manual_subtitles(project_paths, config)
            set_export_job(
                status="rendering",
                detail="Rendering video",
                progress=15,
                started_at=job_started_at,
                project_id=project_key,
                subtitles_ass=str(subtitles_path),
                error=None,
            )
            audio_duration = _audio_duration_seconds(render_audio_path)
            estimated_render_seconds = min(max(audio_duration * 2.2, 120.0), 3600.0) if audio_duration > 0 else 600.0
            set_export_job(
                status="rendering",
                detail="Rendering video",
                progress=15,
                started_at=job_started_at,
                estimated_total_seconds=int(round(estimated_render_seconds)),
                project_id=project_key,
                subtitles_ass=str(subtitles_path),
                error=None,
            )
            render_started_at = time.perf_counter()
            render_stop = threading.Event()
            render_progress = {"ratio": 0.0, "detail": "Rendering video", "last_update": render_started_at}

            def render_heartbeat() -> None:
                while not render_stop.wait(1.0):
                    elapsed = time.perf_counter() - render_started_at
                    estimated_ratio = min(max(elapsed / estimated_render_seconds, 0.0), 1.0)
                    target_progress = max(15, min(int(15 + estimated_ratio * 75), 90))
                    if target_progress <= 15:
                        continue
                    if time.perf_counter() - float(render_progress["last_update"]) < 1.5:
                        continue
                    set_export_job(
                        status="rendering",
                        detail=f"Rendering video ({int(elapsed)}s)",
                        progress=target_progress,
                        started_at=job_started_at,
                        estimated_total_seconds=int(round(estimated_render_seconds)),
                        project_id=project_key,
                        subtitles_ass=str(subtitles_path),
                        error=None,
                    )

            def handle_render_progress(ratio: float, detail: str) -> None:
                render_progress.update(
                    {
                        "ratio": ratio,
                        "detail": detail,
                        "last_update": time.perf_counter(),
                    }
                )
                set_export_job(
                    status="rendering",
                    detail="Finalizing video" if detail == "finalizing" else "Rendering video",
                    progress=max(15, min(int(15 + ratio * 84), 99)),
                    started_at=job_started_at,
                    estimated_total_seconds=int(round(estimated_render_seconds)),
                    project_id=project_key,
                    subtitles_ass=str(subtitles_path),
                    error=None,
                )

            heartbeat_thread = threading.Thread(target=render_heartbeat, daemon=True)
            heartbeat_thread.start()
            output_video = render_video(
                render_audio_path,
                subtitles_path,
                render_config,
                progress_callback=handle_render_progress,
            )
            render_stop.set()
            heartbeat_thread.join(timeout=1.0)
            set_export_job(
                status="completed",
                detail="Completed",
                progress=100,
                started_at=job_started_at,
                estimated_total_seconds=int(round(estimated_render_seconds)),
                project_id=project_key,
                subtitles_ass=str(subtitles_path),
                output_video=str(output_video),
                error=None,
            )
        except Exception as exc:
            if "render_stop" in locals():
                render_stop.set()
            set_export_job(status="error", detail="Failed", project_id=project_key, error=str(exc))

    def set_export_job(**updates: Any) -> None:
        with app.config["EXPORT_LOCK"]:
            job = dict(app.config["EXPORT_JOB"])
            job.update(updates)
            app.config["EXPORT_JOB"] = job

    @app.get("/")
    @app.get("/ui/timing_editor.html")
    def editor_index() -> Response:
        if not base_paths.ui_path.exists():
            return _json_response({"error": f"UI file not found: {base_paths.ui_path}"}, status=404)
        return send_file(base_paths.ui_path)

    @app.get("/api/manifest")
    def api_manifest() -> Response:
        project_paths = current_paths()
        if not project_paths.editor_manifest_path.exists() and not project_paths.manifest_path.exists():
            return _json_response({"error": f"Manifest not found: {project_paths.editor_manifest_path}"}, status=404)
        return _json_response(_read_manifest(project_paths))

    @app.get("/api/overrides")
    def api_overrides() -> Response:
        return _json_response(_read_overrides(current_paths()))

    @app.post("/api/overrides")
    def api_save_overrides() -> Response:
        project_paths = current_paths()
        raw_payload = request.get_json(silent=True)
        if not isinstance(raw_payload, Mapping):
            return _json_response({"error": "Request body must be a JSON object"}, status=400)
        try:
            payload = _sanitize_override_body(raw_payload)
        except ValueError as exc:
            return _json_response({"error": str(exc)}, status=400)
        _write_json_atomic(project_paths.overrides_path, payload)
        if "lyrics_text" in raw_payload:
            _save_project_lyrics(project_paths, str(raw_payload.get("lyrics_text", "")).strip())
        return _json_response(payload)

    @app.get("/api/audio")
    def api_audio() -> Response:
        project_paths = current_paths()
        if not project_paths.audio_path.exists():
            return _json_response({"error": f"Audio file not found: {project_paths.audio_path}"}, status=404)
        return send_file(project_paths.audio_path, mimetype="audio/wav", conditional=True)

    @app.get("/api/waveform")
    def api_waveform() -> Response:
        project_paths = current_paths()
        if not project_paths.audio_path.exists():
            return _json_response({"error": f"Audio file not found: {project_paths.audio_path}"}, status=404)
        bins = request.args.get("bins", default=1400, type=int)
        bins = max(100, min(bins, 4000))
        return _json_response(_waveform_payload(project_paths.audio_path, bins))

    @app.get("/api/projects")
    def api_projects() -> Response:
        current_project_id = _active_project_key(base_paths) or LEGACY_PROJECT_KEY
        return _json_response(
            {
                "ok": True,
                "current_project_id": current_project_id,
                "projects": _list_projects(base_paths),
            }
        )

    @app.post("/api/projects/select")
    def api_projects_select() -> Response:
        raw_payload = request.get_json(silent=True)
        if not isinstance(raw_payload, Mapping):
            return _json_response({"error": "Request body must be a JSON object"}, status=400)
        project_id = str(raw_payload.get("project_id", "")).strip()
        available_ids = {project["id"] for project in _list_projects(base_paths)}
        if project_id not in available_ids:
            return _json_response({"error": f"Unknown project: {project_id}"}, status=404)
        _set_active_project_key(base_paths, project_id)
        project_paths = _project_paths(base_paths, project_id)
        return _json_response({"ok": True, "project_id": project_id, "project_name": _project_display_name(project_paths, project_id)})

    @app.post("/api/projects/create")
    def api_projects_create() -> Response:
        raw_payload = request.get_json(silent=True)
        if not isinstance(raw_payload, Mapping):
            return _json_response({"error": "Request body must be a JSON object"}, status=400)
        project_name = str(raw_payload.get("project_name", "")).strip()
        if not project_name:
            project_name = f"Project {datetime.now().strftime('%Y-%m-%d %H-%M-%S')}"
        lyrics_text = str(raw_payload.get("lyrics_text", "")).strip()
        project_key = _project_storage_key(project_name, "project")
        project_paths = _project_paths(base_paths, project_key)
        if project_paths.state_path.exists():
            return _json_response({"error": f"Project already exists: {project_name}"}, status=409)
        state = _write_empty_project(project_paths, project_name, lyrics_text)
        _set_active_project_key(base_paths, project_key)
        return _json_response(
            {
                "ok": True,
                "project_id": project_key,
                "project_name": project_name,
                "state": state,
                "manifest_path": str(project_paths.editor_manifest_path),
                "overrides_path": str(project_paths.overrides_path),
            }
        )

    @app.get("/api/session")
    def api_session() -> Response:
        project_key = _active_project_key(base_paths) or LEGACY_PROJECT_KEY
        project_paths = _project_paths(base_paths, project_key)
        state = {}
        if project_paths.state_path.exists():
            try:
                state = _load_json_file(project_paths.state_path)
            except Exception:
                state = {}
        lyrics_text = _project_lyrics_text(project_paths)
        source_name = _guess_input_audio_name(project_paths, state)
        payload = {
            "version": 1,
            "mode": state.get("mode", "manual"),
            "status": state.get("status", "empty"),
            "manifest_url": "/api/manifest",
            "overrides_url": "/api/overrides",
            "audio_url": "/api/audio",
            "waveform_url": "/api/waveform",
            "manifest_path": str(project_paths.editor_manifest_path),
            "overrides_path": str(project_paths.overrides_path),
            "audio_path": str(project_paths.audio_path),
            "state_path": str(project_paths.state_path),
            "input_dir": str(project_paths.input_dir),
            "project_id": project_key,
            "project_name": _project_display_name(project_paths, project_key),
            "source_name": source_name,
            "original_audio_name": str(state.get("original_audio_name", "")).strip(),
            "lyrics_text": lyrics_text,
            "output_video_name": _manual_output_video_name(project_paths),
            "output_video_path": str(_default_output_video_path(project_paths)),
        }
        return _json_response(payload)

    @app.post("/api/import")
    def api_import() -> Response:
        audio_file = request.files.get("audio_file")
        if audio_file is None or not audio_file.filename:
            return _json_response({"error": "audio_file is required"}, status=400)

        lyrics_text = str(request.form.get("lyrics_text", "")).strip()
        lyrics_file = request.files.get("lyrics_file")
        if lyrics_file is not None and lyrics_file.filename:
            lyrics_text = lyrics_file.read().decode("utf-8", errors="replace").strip()

        project_name = str(request.form.get("project_name", "")).strip() or Path(audio_file.filename).stem
        project_key = _project_storage_key(project_name, "project")
        project_paths = _project_paths(base_paths, project_key)
        _set_active_project_key(base_paths, project_key)

        source_name = _sanitize_filename(audio_file.filename, "manual_input.mp3")
        source_path = project_paths.input_dir / source_name
        source_path.parent.mkdir(parents=True, exist_ok=True)
        audio_file.save(str(source_path))

        state = _write_manual_project(
            paths=project_paths,
            audio_source=source_path,
            lyrics_text=lyrics_text,
            config_path=project_paths.config_path,
            original_name=audio_file.filename,
            project_name=project_name,
        )
        return _json_response(
            {
                "ok": True,
                "project_id": project_key,
                "project_name": project_name,
                "state": state,
                "manifest_path": str(project_paths.editor_manifest_path),
                "audio_path": str(project_paths.audio_path),
                "overrides_path": str(project_paths.overrides_path),
            }
        )

    @app.post("/api/export/mp4")
    def api_export_mp4() -> Response:
        project_key = _active_project_key(base_paths) or LEGACY_PROJECT_KEY
        project_paths = _project_paths(base_paths, project_key)
        if not project_paths.editor_manifest_path.exists() and not project_paths.manifest_path.exists():
            return _json_response({"error": f"Manifest not found: {project_paths.editor_manifest_path}"}, status=404)
        if not project_paths.audio_path.exists():
            return _json_response({"error": f"Audio file not found: {project_paths.audio_path}"}, status=404)

        if app.testing:
            try:
                config = load_config(project_paths.config_path)
                output_video_name = _manual_output_video_name(project_paths)
                render_config = dict(config)
                renderer_config = dict(render_config.get("renderer", {}))
                renderer_config["output_video_name"] = output_video_name
                renderer_config["audio_artifact"] = "no_vocals_wav"
                render_config["renderer"] = renderer_config
                render_audio_path = _ensure_project_backing_track(project_paths)
                subtitles_path = _build_manual_subtitles(project_paths, config)
                output_video = render_video(render_audio_path, subtitles_path, render_config)
            except Exception as exc:
                return _json_response({"error": str(exc)}, status=500)

            return _json_response(
                {
                    "ok": True,
                    "status": "completed",
                    "detail": "Completed",
                    "progress": 100,
                    "subtitles_ass": str(subtitles_path),
                    "output_video": str(output_video),
                }
            )

        with app.config["EXPORT_LOCK"]:
            current_job = dict(app.config["EXPORT_JOB"])
            if current_job.get("status") in {"queued", "building_subtitles", "rendering"}:
                return _json_response({"ok": True, **current_job}, status=202)
            app.config["EXPORT_JOB"] = {
                "status": "queued",
                "detail": "Queued",
                "progress": 0,
                "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "estimated_total_seconds": None,
                "project_id": project_key,
                "output_video": None,
                "subtitles_ass": None,
                "error": None,
            }

        worker = threading.Thread(target=run_export_job, args=(project_paths, project_key), daemon=True)
        worker.start()
        return _json_response({"ok": True, **app.config["EXPORT_JOB"]}, status=202)

    @app.get("/api/export/status")
    def api_export_status() -> Response:
        project_paths = current_paths()
        with app.config["EXPORT_LOCK"]:
            job = dict(app.config["EXPORT_JOB"])
        if not job.get("output_video"):
            fallback_output = _default_output_video_path(project_paths)
            if fallback_output.exists():
                job["output_video"] = str(fallback_output)
        return _json_response({"ok": True, **job})

    @app.post("/api/output/open")
    def api_output_open() -> Response:
        project_paths = current_paths()
        with app.config["EXPORT_LOCK"]:
            job = dict(app.config["EXPORT_JOB"])

        output_path_raw = job.get("output_video")
        output_path = Path(output_path_raw) if output_path_raw else _default_output_video_path(project_paths)
        if not output_path.exists():
            return _json_response({"error": f"Output video not found: {output_path}"}, status=404)

        try:
            if os.name == "nt":
                os.startfile(str(output_path))  # type: ignore[attr-defined]
            else:
                webbrowser.open(output_path.resolve().as_uri())
        except Exception as exc:
            return _json_response({"error": f"Could not open output video: {exc}"}, status=500)

        return _json_response({"ok": True, "output_video": str(output_path)})

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the manual karaoke timing editor")
    parser.add_argument("--config", default="config.yaml", help="Path to the pipeline config file")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind the local editor server")
    parser.add_argument("--port", type=int, default=8765, help="Port to bind the local editor server")
    parser.add_argument("--debug", action="store_true", help="Enable Flask debug mode")
    args = parser.parse_args()

    app = create_app(args.config)
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
