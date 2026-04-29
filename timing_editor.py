"""Flask-backed timing editor for manual Hebrew karaoke subtitle correction."""

from __future__ import annotations

import argparse
import audioop
import multiprocessing as mp
import html
import json
import logging
import os
import re
import signal
import shutil
import subprocess
import threading
import time
import uuid
import wave
import webbrowser
from urllib.parse import urljoin, urlparse
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, request, send_file

from main import load_config
from modules.audio_extractor import extract_and_normalize_audio
from modules.first_pass import run_first_pass_autosync
from modules.renderer import render_video
from modules.separator import separate_vocals
from modules.subtitle_builder import (
    _ass_placeholder_line,
    _build_image_events,
    _append_countdown_events,
    build_ass_dialogue_lines,
    build_ass_header,
    build_subtitle_settings,
)

LOGGER = logging.getLogger(__name__)


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

    def ensure_directories(self) -> None:
        self.input_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.audio_path.parent.mkdir(parents=True, exist_ok=True)


LEGACY_PROJECT_KEY = "__legacy__"
PIPELINE_STAGE_DEFINITIONS = [
    ("download_convert", "Download & convert"),
    ("lyrics_fetch", "Lyrics fetch"),
    ("stem_separation", "Stem separation"),
    ("transcription", "Transcription"),
    ("lm_studio_correction", "LM Studio correction"),
    ("whisperx_alignment", "WhisperX alignment"),
    ("editor_project", "Building editor project"),
]


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
    input_dir = _resolve_from_config(config_dir, str(paths.get("input_dir", "data/input")))
    temp_dir = _resolve_from_config(config_dir, str(paths.get("temp_dir", "temp")))
    manifest_name = str(subtitle_builder.get("manifest_name", "subtitles_manifest.json"))
    overrides_name = str(subtitle_builder.get("timing_overrides_name", "timing_overrides.json"))
    ui_path = (config_dir / "ui" / "timing_editor.html").resolve()
    
    return EditorPaths(
        config_path=resolved_config,
        root_dir=config_dir,
        input_dir=input_dir,
        temp_dir=temp_dir,
        manifest_path=temp_dir / "subtitles" / manifest_name,
        editor_manifest_path=temp_dir / "subtitles" / "timing_editor_manifest.json",
        overrides_path=temp_dir / "state" / overrides_name,
        state_path=temp_dir / "state" / "state.json",
        audio_path=temp_dir / "audio" / "audio.wav",
        ui_path=ui_path,
    )


def _projects_root(base_paths: EditorPaths) -> Path:
    return base_paths.temp_dir / "projects"


def _current_project_marker(base_paths: EditorPaths) -> Path:
    return base_paths.root_dir / "data" / ".current_project"


def _pipeline_status_path(base_paths: EditorPaths) -> Path:
    return base_paths.temp_dir / "state" / "pipeline_state.json"


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
        manifest_path=temp_dir / "subtitles" / base_paths.manifest_path.name,
        editor_manifest_path=temp_dir / "subtitles" / base_paths.editor_manifest_path.name,
        overrides_path=temp_dir / "state" / base_paths.overrides_path.name,
        state_path=temp_dir / "state" / base_paths.state_path.name,
        audio_path=temp_dir / "audio" / base_paths.audio_path.name,
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


def _default_pipeline_job() -> dict[str, Any]:
    return {
        "version": 1,
        "job_id": "",
        "status": "idle",
        "project_id": "",
        "project_name": "",
        "current_stage_key": "",
        "current_stage_label": "",
        "stage_started_at": None,
        "stage_elapsed_seconds": 0.0,
        "started_at": None,
        "updated_at": None,
        "error": None,
        "stages": [
            {
                "key": key,
                "label": label,
                "status": "pending",
                "detail": "",
                "started_at": None,
                "ended_at": None,
                "elapsed_seconds": 0.0,
            }
            for key, label in PIPELINE_STAGE_DEFINITIONS
        ],
    }


def _read_pipeline_job(base_paths: EditorPaths) -> dict[str, Any]:
    path = _pipeline_status_path(base_paths)
    if not path.exists():
        return _default_pipeline_job()
    try:
        loaded = _load_json_file(path)
    except Exception:
        return _default_pipeline_job()
    if not isinstance(loaded, dict):
        return _default_pipeline_job()
    job = _default_pipeline_job()
    job.update(loaded)
    if not isinstance(job.get("stages"), list) or not job["stages"]:
        job["stages"] = _default_pipeline_job()["stages"]
    if _pipeline_job_is_stale(job):
        LOGGER.warning("Resetting stale pipeline job state: %s", json.dumps(job, ensure_ascii=False, default=str))
        job = _default_pipeline_job()
        _write_pipeline_job(base_paths, job)
    return job


def _pipeline_job_is_stale(job: Mapping[str, Any]) -> bool:
    if str(job.get("status", "")).strip().lower() != "running":
        return False

    stages = job.get("stages", [])
    running_stage = None
    for stage in stages if isinstance(stages, list) else []:
        if not isinstance(stage, Mapping):
            continue
        if str(stage.get("status", "")).strip().lower() == "running":
            running_stage = stage
            break

    if running_stage is not None:
        return False

    updated_at = str(job.get("updated_at", "")).strip()
    started_at = str(job.get("started_at", "")).strip()
    reference_time = updated_at or started_at
    if not reference_time:
        return True
    try:
        reference_dt = datetime.fromisoformat(reference_time.replace("Z", "+00:00"))
    except Exception:
        return True
    age_seconds = max((datetime.now(timezone.utc) - reference_dt).total_seconds(), 0.0)
    return age_seconds > 300.0


def _write_pipeline_job(base_paths: EditorPaths, job: Mapping[str, Any]) -> None:
    _write_json_atomic(_pipeline_status_path(base_paths), job)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _decorate_pipeline_job(job: Mapping[str, Any]) -> dict[str, Any]:
    decorated = dict(job)
    stages = []
    current_stage_key = str(decorated.get("current_stage_key", "")).strip()
    stage_started_at = str(decorated.get("stage_started_at", "")).strip()
    if current_stage_key and stage_started_at:
        try:
            started_at = datetime.fromisoformat(stage_started_at.replace("Z", "+00:00"))
            decorated["stage_elapsed_seconds"] = max((datetime.now(timezone.utc) - started_at).total_seconds(), 0.0)
        except Exception:
            pass

    for stage in decorated.get("stages", []):
        if not isinstance(stage, Mapping):
            continue
        stage_payload = dict(stage)
        if str(stage_payload.get("status", "")).strip() == "running" and str(stage_payload.get("started_at", "")).strip():
            try:
                started_at = datetime.fromisoformat(str(stage_payload["started_at"]).replace("Z", "+00:00"))
                stage_payload["elapsed_seconds"] = max((datetime.now(timezone.utc) - started_at).total_seconds(), 0.0)
            except Exception:
                pass
        stages.append(stage_payload)
    decorated["stages"] = stages
    return decorated


def _update_pipeline_stage(
    base_paths: EditorPaths,
    job: dict[str, Any],
    stage_key: str,
    status: str,
    detail: str = "",
    *,
    started_at: str | None = None,
) -> dict[str, Any]:
    updated = dict(job)
    updated["updated_at"] = _timestamp()
    stages: list[dict[str, Any]] = []
    for stage in job.get("stages", []):
        if not isinstance(stage, Mapping):
            continue
        stage_payload = dict(stage)
        if stage_payload.get("key") == stage_key:
            stage_payload["status"] = status
            stage_payload["detail"] = detail
            if status == "running":
                stage_payload["started_at"] = started_at or stage_payload.get("started_at") or _timestamp()
                stage_payload["ended_at"] = None
                stage_payload["elapsed_seconds"] = 0.0
            elif status in {"done", "skipped"}:
                stage_payload["ended_at"] = _timestamp()
                started_value = str(stage_payload.get("started_at", "")).strip()
                if started_value:
                    try:
                        started_dt = datetime.fromisoformat(started_value.replace("Z", "+00:00"))
                        stage_payload["elapsed_seconds"] = max((datetime.now(timezone.utc) - started_dt).total_seconds(), 0.0)
                    except Exception:
                        stage_payload["elapsed_seconds"] = 0.0
            elif status == "error":
                stage_payload["ended_at"] = _timestamp()
            updated["current_stage_key"] = stage_key if status == "running" else ""
            updated["current_stage_label"] = str(stage_payload.get("label", ""))
            updated["stage_started_at"] = stage_payload.get("started_at") if status == "running" else None
        stages.append(stage_payload)
    updated["stages"] = stages
    _write_pipeline_job(base_paths, updated)
    return updated


def _json_response(payload: Mapping[str, Any], status: int = 200) -> Response:
    return Response(
        json.dumps(payload, ensure_ascii=False, indent=2),
        status=status,
        content_type="application/json; charset=utf-8",
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


def _clean_media_display_name(raw: str) -> str:
    cleaned = Path(raw).stem.strip() if raw else ""
    if not cleaned:
        return ""
    cleaned = re.sub(r"\s*\[[^\]]+\]\s*$", "", cleaned)
    cleaned = re.sub(r"\s*\([^)]*\)\s*$", "", cleaned)
    cleaned = re.sub(r"\s+\((?:youtube|karaoke|official(?:\s+video)?|lyrics?)\)$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*-\s*מוזיקה ישראלית$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*-\s*ישראלית$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -")
    return cleaned


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
    return paths.temp_dir / "subtitles" / "lyrics.txt"


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
    
    # Flattened words from the NEW text
    new_words_flat = [token for line in cleaned_lines for token in _split_words(line)]
    
    existing_words = manifest.get("words", [])
    if not isinstance(existing_words, list) or len(new_words_flat) != len(existing_words):
        LOGGER.warning(f"_sync_manifest_text_with_lyrics: word count mismatch ({len(new_words_flat)} vs {len(existing_words)}). Skipping sync.")
        return

    # Update word texts in place to match the new lyrics (preserving their IDs and timing)
    for index, word in enumerate(existing_words):
        if isinstance(word, dict):
            word["text"] = new_words_flat[index]

    # Rebuild the lines array to match the new line breaks in lyrics_text
    new_lines = []
    word_ptr = 0
    for l_idx, line_text in enumerate(cleaned_lines):
        line_tokens = _split_words(line_text)
        line_word_ids = []
        
        # Grab the words belonging to this line from the existing pool
        current_line_words = []
        for _ in line_tokens:
            if word_ptr < len(existing_words):
                w = existing_words[word_ptr]
                line_word_ids.append(w.get("id"))
                current_line_words.append(w)
                word_ptr += 1
        
        # Calculate line start/end boundaries from its constituent words
        l_start = current_line_words[0].get("start", 0) if current_line_words else 0
        l_end = current_line_words[-1].get("end", 0.1) if current_line_words else 0.1
        
        new_lines.append({
            "id": f"line_{l_idx:03d}",
            "index": l_idx,
            "text": line_text,
            "start": l_start,
            "end": l_end,
            "word_ids": line_word_ids
        })
    
    manifest["lines"] = new_lines
    _write_json_atomic(paths.editor_manifest_path, manifest)
    LOGGER.info(f"_sync_manifest_text_with_lyrics: manifest lines updated to {len(new_lines)} lines")


def _rebuild_manifest_from_lyrics(paths: EditorPaths, lyrics_text: str) -> None:
    cleaned_lines = _clean_text_lines(lyrics_text)
    duration_seconds = _read_wav_duration(paths.audio_path) if paths.audio_path.exists() else float(
        max(sum(len(_split_words(line)) for line in cleaned_lines), 0)
    )
    words, lines_payload = _build_word_stream(cleaned_lines, duration_seconds)
    manifest = {"version": 1, "lines": lines_payload, "words": words, "intro": _project_intro_metadata(paths)}
    _write_json_atomic(paths.editor_manifest_path, manifest)

    overrides = _read_overrides(paths)
    overrides["placed_word_count"] = 0
    overrides["words"] = {}
    overrides["lines"] = {}
    overrides["lyrics_text"] = "\n".join(cleaned_lines)
    _write_json_atomic(paths.overrides_path, overrides)

    state = _state_payload(paths)
    if state:
        state["line_count"] = len(lines_payload)
        state["word_count"] = len(words)
        state["lyrics_text"] = "\n".join(cleaned_lines)
        state["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        _write_json_atomic(paths.state_path, state)


def _ensure_manifest_consistency(paths: EditorPaths) -> None:
    overrides = _read_overrides(paths)
    if int(overrides.get("placed_word_count", 0) or 0) > 0:
        return

    lyrics_text = str(overrides.get("lyrics_text", "")).strip() or _project_lyrics_text(paths)
    if not lyrics_text:
        return

    cleaned_lines = _clean_text_lines(lyrics_text)
    flattened_words = [token for line in cleaned_lines for token in _split_words(line)]
    if not paths.editor_manifest_path.exists() and not paths.manifest_path.exists():
        _rebuild_manifest_from_lyrics(paths, lyrics_text)
        return

    manifest = _read_manifest(paths)
    existing_words = manifest.get("words", [])
    if not isinstance(existing_words, list) or len(existing_words) != len(flattened_words):
        _rebuild_manifest_from_lyrics(paths, lyrics_text)
        return


def _project_intro_metadata(paths: EditorPaths) -> dict[str, Any]:
    state = _state_payload(paths)
    project_name = str(state.get("project_name", "")).strip()
    source_name = str(state.get("source_name", "")).strip()
    original_audio_name = str(state.get("original_audio_name", "")).strip()
    source_stem = _clean_media_display_name(source_name or original_audio_name)
    project_title = re.sub(r"\s+\(Kareoke\)$", "", project_name, flags=re.IGNORECASE).strip()
    title = project_title or source_stem
    subtitle = ""
    return {
        "intro_title": title,
        "intro_subtitle": subtitle,
        "intro_duration_seconds": 2.5,
        "intro_font_multiplier": 2.0,
    }


def _save_project_lyrics(paths: EditorPaths, lyrics_text: str) -> None:
    cleaned_text = "\n".join(_clean_text_lines(lyrics_text))
    _lyrics_path(paths).write_text(cleaned_text, encoding="utf-8")

    state = _state_payload(paths)
    if state:
        state["lyrics_text"] = cleaned_text
        state["line_count"] = len(_clean_text_lines(cleaned_text))
        state["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        _write_json_atomic(paths.state_path, state)

    overrides = _read_overrides(paths)
    current_word_count = 0
    if paths.editor_manifest_path.exists() or paths.manifest_path.exists():
        try:
            manifest = _read_manifest(paths)
            raw_words = manifest.get("words", [])
            current_word_count = len(raw_words) if isinstance(raw_words, list) else 0
        except Exception:
            current_word_count = 0
    next_word_count = sum(len(_split_words(line)) for line in _clean_text_lines(cleaned_text))

    if int(overrides.get("placed_word_count", 0) or 0) == 0 or next_word_count != current_word_count:
        _rebuild_manifest_from_lyrics(paths, cleaned_text)
    else:
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


def _rewrite_nested_path_strings(value: Any, old_prefix: str, new_prefix: str) -> Any:
    if isinstance(value, str):
        return value.replace(old_prefix, new_prefix, 1) if value.startswith(old_prefix) else value
    if isinstance(value, list):
        return [_rewrite_nested_path_strings(item, old_prefix, new_prefix) for item in value]
    if isinstance(value, Mapping):
        return {
            str(key): _rewrite_nested_path_strings(item, old_prefix, new_prefix)
            for key, item in value.items()
        }
    return value


def _refresh_project_intro(paths: EditorPaths) -> None:
    if paths.editor_manifest_path.exists():
        manifest = _load_json_file(paths.editor_manifest_path)
        manifest["intro"] = _project_intro_metadata(paths)
        _write_json_atomic(paths.editor_manifest_path, manifest)


def _update_project_state_name(paths: EditorPaths, project_name: str) -> None:
    state = _state_payload(paths)
    if not state:
        return
    state["project_name"] = str(project_name).strip()
    state["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _write_json_atomic(paths.state_path, state)
    _refresh_project_intro(paths)


def _rename_project(base_paths: EditorPaths, project_id: str, project_name: str) -> tuple[str, EditorPaths]:
    cleaned_name = str(project_name).strip()
    if not cleaned_name:
        raise ValueError("project_name is required")
    if not project_id or project_id == LEGACY_PROJECT_KEY:
        raise ValueError("Only saved projects can be renamed")

    old_paths = _project_paths(base_paths, project_id)
    if not old_paths.temp_dir.exists():
        raise FileNotFoundError(f"Project directory not found: {old_paths.temp_dir}")

    new_project_id = _project_storage_key(cleaned_name, "project")
    new_paths = _project_paths(base_paths, new_project_id)
    if new_project_id != project_id and new_paths.temp_dir.exists():
        raise ValueError(f"Project already exists: {cleaned_name}")

    if new_project_id == project_id:
        _update_project_state_name(old_paths, cleaned_name)
        return new_project_id, old_paths

    old_prefix = str(old_paths.temp_dir.resolve())
    new_prefix = str(new_paths.temp_dir.resolve())
    old_paths.temp_dir.replace(new_paths.temp_dir)

    if new_paths.state_path.exists():
        state = _load_json_file(new_paths.state_path)
        state = _rewrite_nested_path_strings(state, old_prefix, new_prefix)
        state["project_name"] = cleaned_name
        state["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        _write_json_atomic(new_paths.state_path, state)

    if new_paths.editor_manifest_path.exists():
        manifest = _load_json_file(new_paths.editor_manifest_path)
        manifest = _rewrite_nested_path_strings(manifest, old_prefix, new_prefix)
        manifest["intro"] = _project_intro_metadata(new_paths)
        _write_json_atomic(new_paths.editor_manifest_path, manifest)

    if new_paths.manifest_path.exists():
        manifest = _load_json_file(new_paths.manifest_path)
        manifest = _rewrite_nested_path_strings(manifest, old_prefix, new_prefix)
        _write_json_atomic(new_paths.manifest_path, manifest)

    _set_active_project_key(base_paths, new_project_id)
    return new_project_id, new_paths


def _delete_project(base_paths: EditorPaths, project_id: str) -> str:
    if not project_id:
        raise ValueError("project_id is required")
    if project_id == LEGACY_PROJECT_KEY:
        raise ValueError("Legacy project deletion is not supported")

    project_paths = _project_paths(base_paths, project_id)
    if not project_paths.temp_dir.exists():
        raise FileNotFoundError(f"Project directory not found: {project_paths.temp_dir}")

    shutil.rmtree(project_paths.temp_dir)

    remaining_projects = _list_projects(base_paths)
    next_project_id = remaining_projects[0]["id"] if remaining_projects else (LEGACY_PROJECT_KEY if _legacy_project_exists(base_paths) else "")
    _set_active_project_key(base_paths, next_project_id)
    return next_project_id


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


def _fetch_youtube_metadata(youtube_url: str) -> dict[str, str]:
    normalized_url = str(youtube_url).strip()
    if not normalized_url:
        raise ValueError("youtube_url is required")
    parsed = urlparse(normalized_url)
    hostname = (parsed.hostname or "").lower()
    if hostname not in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}:
        raise ValueError("YouTube URL field only accepts youtube.com or youtu.be links")

    command = [
        "yt-dlp",
        "--dump-single-json",
        "--no-playlist",
        "--no-warnings",
        normalized_url,
    ]

    try:
        completed = subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError("yt-dlp is not installed or not available on PATH") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        stdout = (exc.stdout or "").strip()
        detail = stderr or stdout or "yt-dlp failed to read YouTube metadata"
        raise RuntimeError(detail) from exc

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("yt-dlp returned invalid metadata JSON") from exc

    if not isinstance(payload, Mapping):
        raise RuntimeError("yt-dlp metadata response was not an object")

    LOGGER.info("yt-dlp info_dict: %s", json.dumps(payload, ensure_ascii=False, indent=2, default=str))

    def read_text(*keys: str) -> str:
        for key in keys:
            value = payload.get(key)
            if value:
                return str(value).strip()
        return ""

    return {
        "title": read_text("title", "fulltitle"),
        "track": read_text("track"),
        "artist": read_text("artist", "creator", "album_artist"),
        "uploader": read_text("uploader", "channel"),
        "channel": read_text("channel", "uploader"),
    }


def _clean_youtube_project_title(raw_title: str) -> str:
    cleaned = html.unescape(str(raw_title or "")).strip()
    if not cleaned:
        return ""

    if "|" in cleaned:
        segments = cleaned.split("|")
        for segment in segments:
            if re.search(r"[\u0590-\u05FF]", segment):
                cleaned = segment.strip()
                break

    cleaned = cleaned.replace("\u2013", "-").replace("\u2014", "-").replace("\u2010", "-")
    suffix_patterns = [
        r"\s*[\(\[]\s*(?:official\s+video|official\s+audio|lyrics?|audio|clip|קליפ)\s*[\)\]]\s*$",
        r"\s*-\s*(?:official\s+video|official\s+audio|lyrics?|audio|clip|קליפ)\s*$",
    ]
    previous = None
    while previous != cleaned:
        previous = cleaned
        for pattern in suffix_patterns:
            cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" -")
    return cleaned


def _download_youtube_audio(paths: EditorPaths, youtube_url: str) -> Path:
    normalized_url = str(youtube_url).strip()
    if not normalized_url:
        raise ValueError("youtube_url is required")
    parsed = urlparse(normalized_url)
    hostname = (parsed.hostname or "").lower()
    if hostname not in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}:
        raise ValueError("YouTube URL field only accepts youtube.com or youtu.be links")

    paths.input_dir.mkdir(parents=True, exist_ok=True)
    output_template = paths.input_dir / "%(title)s [%(id)s].%(ext)s"
    command = [
        "yt-dlp",
        "--no-playlist",
        "--extract-audio",
        "--audio-format",
        "mp3",
        "--audio-quality",
        "0",
        "--no-progress",
        "--print",
        "after_move:filepath",
        "--output",
        str(output_template),
        normalized_url,
    ]

    try:
        completed = subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError("yt-dlp is not installed or not available on PATH") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        stdout = (exc.stdout or "").strip()
        detail = stderr or stdout or "yt-dlp failed to download the requested audio"
        raise RuntimeError(detail) from exc

    output_lines = [
        line.strip().strip('"')
        for line in (completed.stdout or "").splitlines()
        if line.strip()
    ]
    for candidate in reversed(output_lines):
        candidate_path = Path(candidate).expanduser()
        if candidate_path.exists():
            return candidate_path.resolve()

    recent_downloads = sorted(
        (path for path in paths.input_dir.glob("*.mp3") if path.is_file()),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    if recent_downloads:
        return recent_downloads[0].resolve()

    raise RuntimeError("yt-dlp completed, but no downloaded MP3 file was found")


def _normalize_lookup_text(raw: str) -> str:
    cleaned = html.unescape(str(raw or "")).replace("\xa0", " ").strip()
    cleaned = cleaned.replace("–", "-").replace("—", "-").replace("|", "-")
    cleaned = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", cleaned)
    cleaned = re.sub(r"\b(?:official|lyrics?|karaoke|audio|video|live|mv|hd)\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -")
    return cleaned


def _lookup_tokens(raw: str) -> set[str]:
    normalized = _normalize_lookup_text(raw).lower()
    return {
        token
        for token in re.split(r"[^0-9a-z\u0590-\u05FF]+", normalized)
        if token
    }


def _text_similarity_score(candidate: str, expected_song: str, expected_artist: str) -> int:
    normalized_candidate = _normalize_lookup_text(candidate).lower()
    candidate_tokens = _lookup_tokens(normalized_candidate)
    song_tokens = _lookup_tokens(expected_song)
    artist_tokens = _lookup_tokens(expected_artist)

    score = 0
    song_phrase = _normalize_lookup_text(expected_song).lower()
    artist_phrase = _normalize_lookup_text(expected_artist).lower()

    if song_phrase and song_phrase in normalized_candidate:
        score += 40
    if artist_phrase and artist_phrase in normalized_candidate:
        score += 28
    if song_tokens:
        score += 8 * len(song_tokens & candidate_tokens)
        if song_tokens.issubset(candidate_tokens):
            score += 16
    if artist_tokens:
        score += 6 * len(artist_tokens & candidate_tokens)
        if artist_tokens.issubset(candidate_tokens):
            score += 12

    return score


def _normalized_song_match_score(candidate: str, expected_song: str, expected_artist: str = "") -> int:
    normalized_candidate = _normalize_lookup_text(candidate).lower()
    normalized_song = _normalize_lookup_text(expected_song).lower()
    normalized_artist = _normalize_lookup_text(expected_artist).lower()
    if not normalized_song:
        return 0
    if normalized_candidate == "featured match":
        return 50
    if normalized_song not in normalized_candidate:
        return 0
    return _text_similarity_score(candidate, expected_song, expected_artist)


def _project_name_from_lyrics_title(raw_title: str) -> str:
    cleaned = html.unescape(str(raw_title or "")).strip()
    if not cleaned:
        return ""
    cleaned = re.sub(r"^\s*\u05de\u05d9\u05dc\u05d9\u05dd\s+\u05dc\u05e9\u05d9\u05e8\s+", "", cleaned)
    cleaned = re.sub(r"\s+».*$", "", cleaned)
    cleaned = cleaned.replace("–", "-").replace("—", "-")
    cleaned = re.sub(r"\s*-\s*", " - ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -")
    return cleaned


def _infer_song_and_artist(youtube_metadata: Mapping[str, str], fallback_name: str) -> tuple[str, str, str]:
    raw_title = (
        str(youtube_metadata.get("track", "")).strip()
        or str(youtube_metadata.get("title", "")).strip()
        or _clean_media_display_name(fallback_name)
        or Path(fallback_name).stem
    )
    normalized_title = _normalize_lookup_text(raw_title)
    artist = (
        str(youtube_metadata.get("artist", "")).strip()
        or str(youtube_metadata.get("uploader", "")).strip()
        or str(youtube_metadata.get("channel", "")).strip()
    )

    segments = [segment.strip() for segment in re.split(r"\s[-/]\s", normalized_title) if segment.strip()]
    if len(segments) >= 2:
        left, right = segments[0], segments[1]
        normalized_artist = _normalize_lookup_text(artist).lower()
        if normalized_artist and normalized_artist in _normalize_lookup_text(left).lower():
            return right, artist, normalized_title
        if normalized_artist and normalized_artist in _normalize_lookup_text(right).lower():
            return left, artist, normalized_title
        inferred_artist = artist or left
        inferred_song = right
        return inferred_song, inferred_artist, normalized_title

    return normalized_title, artist, normalized_title


def _youtube_project_name_from_metadata(youtube_metadata: Mapping[str, str], fallback_name: str) -> str:
    cleaned_title = _clean_youtube_project_title(str(youtube_metadata.get("title", "")).strip())
    if cleaned_title:
        return cleaned_title
    fallback_title = _clean_media_display_name(fallback_name)
    return fallback_title or Path(fallback_name).stem.strip()


def _search_shirrim_results(query: str) -> list[dict[str, str]]:
    search_query = str(query).strip()
    if not search_query:
        return []

    try:
        response = requests.get(
            "https://shirrim.com/singers/israel-singers/",
            params={"s": search_query},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=20,
        )
        response.raise_for_status()
    except requests.RequestException:
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    seen_urls: set[str] = set()
    results: list[dict[str, str]] = []
    for link in soup.select('a[href*="/song-lyrics/"]'):
        href = str(link.get("href", "")).strip()
        if not href or href.endswith("/song-lyrics/") or href in seen_urls:
            continue
        seen_urls.add(href)
        title = link.get_text(" ", strip=True)
        if not title:
            continue
        results.append({"title": title, "url": href, "query": search_query})
    return results









def _normalize_lookup_text(raw: str) -> str:
    cleaned = html.unescape(str(raw or "")).replace("\xa0", " ").strip()
    cleaned = cleaned.replace("\u2013", "-").replace("\u2014", "-").replace("|", "-")
    cleaned = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", cleaned)
    cleaned = re.sub(r"https?://\S+", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"\b(?:official|lyrics?|karaoke|audio|video|live|mv|hd|prod|production|version|remaster(?:ed)?)\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -")
    return cleaned


def _hebrew_only_text(raw: str) -> str:
    cleaned = _normalize_lookup_text(raw)
    cleaned = re.sub(r"[^0-9\u0590-\u05FF'\"׳״/\- ]+", " ", cleaned)
    cleaned = cleaned.replace("/", " - ")
    cleaned = re.sub(r"\s*-\s*", " - ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -")
    return cleaned


def _hebrew_dash_title(raw: str) -> str:
    normalized = _normalize_lookup_text(raw)
    segments = [segment.strip() for segment in re.split(r"\s*-\s*", normalized) if segment.strip()]
    hebrew_segments: list[str] = []
    for segment in segments:
        hebrew = _hebrew_only_text(segment)
        if len(re.findall(r"[\u0590-\u05FF]", hebrew)) >= 2 and hebrew not in hebrew_segments:
            hebrew_segments.append(hebrew)
    if len(hebrew_segments) >= 2:
        return " - ".join(hebrew_segments[:2])
    if hebrew_segments:
        return hebrew_segments[0]
    return ""


def _preferred_source_audio_name(project_name: str, fallback_name: str) -> str:
    preferred_stem = (
        _hebrew_dash_title(project_name)
        or _project_name_from_lyrics_title(project_name)
        or _hebrew_dash_title(fallback_name)
        or _clean_media_display_name(fallback_name)
        or Path(fallback_name).stem.strip()
        or "manual_input"
    )
    suffix = Path(fallback_name).suffix.strip() or ".mp3"
    return f"{preferred_stem}{suffix}"


def _lookup_tokens(raw: str) -> set[str]:
    normalized = _normalize_lookup_text(raw).lower()
    return {
        token
        for token in re.split(r"[^0-9a-z\u0590-\u05FF]+", normalized)
        if token
    }


def _text_similarity_score(candidate: str, expected_song: str, expected_artist: str) -> int:
    normalized_candidate = _normalize_lookup_text(candidate).lower()
    candidate_tokens = _lookup_tokens(normalized_candidate)
    song_tokens = _lookup_tokens(expected_song)
    artist_tokens = _lookup_tokens(expected_artist)

    score = 0
    song_phrase = _normalize_lookup_text(expected_song).lower()
    artist_phrase = _normalize_lookup_text(expected_artist).lower()

    if song_phrase and song_phrase in normalized_candidate:
        score += 40
    if artist_phrase and artist_phrase in normalized_candidate:
        score += 28
    if song_tokens:
        score += 8 * len(song_tokens & candidate_tokens)
        if song_tokens.issubset(candidate_tokens):
            score += 16
    if artist_tokens:
        score += 6 * len(artist_tokens & candidate_tokens)
        if artist_tokens.issubset(candidate_tokens):
            score += 12

    return score


def _project_name_from_lyrics_title(raw_title: str) -> str:
    cleaned = html.unescape(str(raw_title or "")).strip()
    if not cleaned:
        return ""
    cleaned = re.sub(r"^\s*\u05de\u05d9\u05dc\u05d9\u05dd\s+\u05dc\u05e9\u05d9\u05e8\s+", "", cleaned)
    cleaned = re.sub(r"\s+».*$", "", cleaned)
    cleaned = cleaned.replace("\u2013", "-").replace("\u2014", "-")
    cleaned = re.sub(r"\s*-\s*", " - ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -")
    hebrew_title = _hebrew_dash_title(cleaned)
    if hebrew_title:
        segments = [segment.strip() for segment in hebrew_title.split(" - ") if segment.strip()]
        if len(segments) >= 2:
            return f"{segments[1]} - {segments[0]}"
        return hebrew_title
    return cleaned


def _infer_song_and_artist(youtube_metadata: Mapping[str, str], fallback_name: str) -> tuple[str, str, str]:
    raw_title = (
        str(youtube_metadata.get("track", "")).strip()
        or str(youtube_metadata.get("title", "")).strip()
        or _clean_media_display_name(fallback_name)
        or Path(fallback_name).stem
    )
    normalized_title = _hebrew_dash_title(raw_title) or _normalize_lookup_text(raw_title)
    artist = (
        str(youtube_metadata.get("artist", "")).strip()
        or str(youtube_metadata.get("uploader", "")).strip()
        or str(youtube_metadata.get("channel", "")).strip()
    )
    artist = _hebrew_only_text(artist) or _normalize_lookup_text(artist)

    segments = [segment.strip() for segment in re.split(r"\s*-\s*", normalized_title) if segment.strip()]
    if len(segments) >= 2:
        left, right = segments[0], segments[1]
        normalized_artist = _normalize_lookup_text(artist).lower()
        if normalized_artist and normalized_artist in _normalize_lookup_text(left).lower():
            return right, artist, normalized_title
        if normalized_artist and normalized_artist in _normalize_lookup_text(right).lower():
            return left, artist, normalized_title
        return left, artist or right, normalized_title

    return normalized_title, artist, normalized_title


def _search_shirrim_results(query: str) -> list[dict[str, str]]:
    search_query = str(query).strip()
    if not search_query:
        return []

    try:
        response = requests.get(
            "https://shirrim.com/singers/israel-singers/",
            params={"s": search_query},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=20,
        )
        response.raise_for_status()
    except requests.RequestException:
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    
    # 1. Try to find the "Best Match" / Featured result first
    # The featured result typically has a button like "למילות השיר ..."
    featured_link = None
    for link in soup.find_all("a", href=True):
        text = link.get_text(" ", strip=True)
        if "למילות השיר" in text and "/song-lyrics/" in link['href']:
            featured_link = link['href']
            break
    
    seen_urls: set[str] = set()
    results: list[dict[str, str]] = []
    
    if featured_link:
        # Add featured result as the very first entry
        results.append({"title": "Featured Match", "url": featured_link, "query": search_query})
        seen_urls.add(featured_link)

    # 2. Find all other song links
    for link in soup.select('a[href*="/song-lyrics/"]'):
        href = str(link.get("href", "")).strip()
        if not href or href in seen_urls:
            continue
        parsed_href = urlparse(href)
        if parsed_href.netloc and parsed_href.netloc not in {"shirrim.com", "www.shirrim.com"}:
            continue
        if not parsed_href.path.startswith("/song-lyrics/") or parsed_href.path.rstrip("/") == "/song-lyrics":
            continue
        seen_urls.add(href)
        title = link.get_text(" ", strip=True)
        if not title:
            continue
        results.append({"title": title, "url": href, "query": search_query})
    return results

def _search_shirrim_artist_page(artist: str, song: str) -> list[dict[str, str]]:
    """Find song links on a specific artist's profile page."""
    if not artist:
        return []
    
    # 1. Find artist slug via search
    artist_slug = ""
    try:
        resp = requests.get(
            "https://shirrim.com/singers/israel-singers/",
            params={"s": artist},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        # Look for links that go to /singers/<slug>/
        for link in soup.select('a[href*="/singers/"]'):
            href = link.get("href", "")
            if "/song-lyrics/" in href: continue # skip songs
            if "israel-singers" in href: continue # skip main page
            link_text = link.get_text().lower()
            if artist.lower() in link_text:
                artist_slug = href.rstrip("/")
                break
    except Exception as exc:
        LOGGER.debug("Failed to find artist slug for %s: %s", artist, exc)

    if not artist_slug:
        return []

    # 2. Fetch artist page and find songs
    try:
        resp = requests.get(
            artist_slug,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        results = []
        for link in soup.select('a[href*="/song-lyrics/"]'):
            title = link.get_text(" ", strip=True)
            if not title: continue
            results.append({"title": title, "url": link.get("href"), "query": song})
        return results
    except Exception:
        return []


def _discover_project_details_from_youtube(youtube_url: str, fallback_name: str) -> dict[str, str]:
    youtube_metadata = _fetch_youtube_metadata(youtube_url)
    raw_video_title = str(youtube_metadata.get("title", "")).strip()
    cleaned_video_title = _clean_youtube_project_title(raw_video_title)
    LOGGER.info("YouTube video title extracted: %s", raw_video_title or "<missing>")
    LOGGER.info("YouTube project title cleaned: %s", cleaned_video_title or "<fallback>")
    expected_song, expected_artist, normalized_title = _infer_song_and_artist(youtube_metadata, fallback_name)
    youtube_project_name = _youtube_project_name_from_metadata(youtube_metadata, fallback_name)
    song_query = _hebrew_only_text(expected_song) or _normalize_lookup_text(expected_song)
    artist_filter = _hebrew_only_text(expected_artist) or _normalize_lookup_text(expected_artist)
    if not song_query:
        return {
            "lyrics_text": "",
            "project_name": "",
            "youtube_project_name": youtube_project_name,
            "lyrics_source_url": "",
            "lyrics_title": "",
            "youtube_title": raw_video_title,
            "youtube_artist": str(youtube_metadata.get("artist", "") or youtube_metadata.get("uploader", "")).strip(),
        }

    scored_results: list[tuple[int, dict[str, str]]] = []
    
    # Strategy 1: Artist Page Search (Most reliable)
    if expected_artist:
        LOGGER.info("Trying artist page search for: %s", expected_artist)
        for result in _search_shirrim_artist_page(expected_artist, expected_song):
            score = _normalized_song_match_score(result["title"], song_query, artist_filter)
            if score > 0:
                scored_results.append((score, result))
    
    # Strategy 2: General Search (Fallback)
    if not scored_results:
        LOGGER.info("Falling back to general search for: %s", song_query)
    scored_results: list[tuple[int, dict[str, str]]] = []
    
    # Try tiered search queries for better recall
    queries = [
        f"{song_query} {artist_filter}".strip(), # Tier 1: Song + Artist
        song_query,                              # Tier 2: Song only (Broadest)
    ]
    
    for query in queries:
        if not query:
            continue
        LOGGER.info("Trying Shirrim search with query: %s", query)
        for result in _search_shirrim_results(query):
            score = _normalized_song_match_score(result["title"], song_query, artist_filter)
            if score > 0:
                scored_results.append((score, result))
        if scored_results:
            break # Found matches with a specific query, stop expanding search


    base_payload = {
        "lyrics_text": "",
        "project_name": "",
        "youtube_project_name": youtube_project_name,
        "lyrics_source_url": "",
        "lyrics_title": "",
        "youtube_title": raw_video_title,
        "youtube_artist": str(youtube_metadata.get("artist", "") or youtube_metadata.get("uploader", "")).strip(),
    }

    if not scored_results:
        LOGGER.info("No suitable song matches found on Shirrim")
        return base_payload

    scored_results.sort(key=lambda item: item[0], reverse=True)
    best_score, best_result = scored_results[0]
    LOGGER.info("Best match: %s (Score: %d)", best_result["title"], best_score)
    if best_score < 40:
        LOGGER.info("Best score %d is below threshold (40)", best_score)
        return base_payload

    try:
        lyrics_payload = _fetch_shirrim_lyrics(best_result["url"])
    except Exception as exc:
        LOGGER.error("Failed to fetch lyrics from %s: %s", best_result["url"], exc)
        return base_payload

    matched_song = str(lyrics_payload.get("title", "")).strip() or str(best_result.get("title", "")).strip()
    matched_score = _normalized_song_match_score(matched_song, song_query, artist_filter)
    if matched_score < 40:
        LOGGER.info("Matched song %s score %d is below threshold (40)", matched_song, matched_score)
        return base_payload

    project_name = (
        _project_name_from_lyrics_title(lyrics_payload.get("title", ""))
        or _project_name_from_lyrics_title(best_result.get("title", ""))
    )
    LOGGER.info("Successfully discovered project: %s", project_name)
    return {
        "lyrics_text": str(lyrics_payload.get("lyrics", "")).strip(),
        "project_name": project_name,
        "youtube_project_name": youtube_project_name,
        "lyrics_source_url": str(lyrics_payload.get("source_url", "")).strip(),
        "lyrics_title": str(lyrics_payload.get("title", "")).strip(),
        "youtube_title": str(youtube_metadata.get("title", "")).strip(),
        "youtube_artist": str(youtube_metadata.get("artist", "") or youtube_metadata.get("uploader", "")).strip(),
    }

    return base_payload


def _discover_project_details_from_audio_filename(audio_filename: str) -> dict[str, str]:
    original_name = str(audio_filename or "").strip()
    source_stem = Path(original_name).stem.strip() or _clean_media_display_name(original_name)
    project_name = source_stem or "Project"

    artist = ""
    song = source_stem
    if " - " in source_stem:
        parts = [segment.strip() for segment in source_stem.split(" - ", 1)]
        if len(parts) == 2 and parts[0] and parts[1]:
            artist, song = parts[0], parts[1]

    song_query = _hebrew_only_text(song) or _normalize_lookup_text(song)
    artist_filter = _hebrew_only_text(artist) or _normalize_lookup_text(artist)

    scored_results: list[tuple[int, dict[str, str]]] = []
    
    # Try tiered search queries for better recall
    queries = [
        f"{song_query} {artist_filter}".strip(), # Tier 1: Song + Artist
        song_query,                              # Tier 2: Song only (Broadest)
    ]
    
    for query in queries:
        if not query:
            continue
        LOGGER.info("Trying Shirrim search with query: %s", query)
        for result in _search_shirrim_results(query):
            score = _normalized_song_match_score(result["title"], song_query, artist_filter)
            if score > 0:
                scored_results.append((score, result))
        if scored_results:
            break # Found matches with a specific query, stop expanding search

    base_payload = {
        "project_name": project_name,
        "lyrics_text": "",
        "lyrics_source_url": "",
        "lyrics_title": "",
        "source_query": song,
        "source_artist": artist,
    }

    if not scored_results:
        return base_payload

    scored_results.sort(key=lambda item: item[0], reverse=True)
    best_score, best_result = scored_results[0]
    LOGGER.info("Best match: %s (Score: %d)", best_result["title"], best_score)
    if best_score < 40:
        LOGGER.info("Best score %d is below threshold (40)", best_score)
        return base_payload

    try:
        lyrics_payload = _fetch_shirrim_lyrics(best_result["url"])
    except Exception as exc:
        LOGGER.error("Failed to fetch lyrics from %s: %s", best_result["url"], exc)
        return base_payload

    return {
        "project_name": project_name,
        "lyrics_text": str(lyrics_payload.get("lyrics", "")).strip(),
        "lyrics_source_url": str(lyrics_payload.get("source_url", "")).strip(),
        "lyrics_title": str(lyrics_payload.get("title", "")).strip(),
        "source_query": song,
        "source_artist": artist,
    }



def _find_shirrim_lyrics_link(soup: BeautifulSoup, base_url: str) -> str:
    for link in soup.find_all("a", href=True):
        href = str(link.get("href", "")).strip()
        if not href:
            continue
        resolved = urljoin(base_url, href)
        parsed = urlparse(resolved)
        hostname = (parsed.hostname or "").lower()
        if hostname not in {"shirrim.com", "www.shirrim.com"}:
            continue
        if not parsed.path.startswith("/song-lyrics/"):
            continue
        text = " ".join(link.get_text(" ", strip=True).split())
        if text.startswith("\u05dc\u05de\u05d9\u05dc\u05d9\u05dd \u05e9\u05dc \u05d4\u05e9\u05d9\u05e8"):
            return resolved
    return ""


def _fetch_shirrim_lyrics(shirrim_url: str) -> dict[str, str]:
    normalized_url = str(shirrim_url).strip()
    if not normalized_url:
        raise ValueError("lyrics_url is required")

    parsed = urlparse(normalized_url)
    hostname = (parsed.hostname or "").lower()
    if hostname not in {"shirrim.com", "www.shirrim.com"}:
        raise ValueError("Only shirrim.com lyrics pages are supported")
    if not parsed.path.startswith("/song-lyrics/") and not parsed.path.startswith("/song-chrods/"):
        raise ValueError("Expected a shirrim.com lyrics or chords page")

    try:
        response = requests.get(
            normalized_url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=20,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Failed to fetch lyrics page: {exc}") from exc

    soup = BeautifulSoup(response.text, "html.parser")
    if parsed.path.startswith("/song-chrods/"):
        lyrics_url = _find_shirrim_lyrics_link(soup, normalized_url)
        if not lyrics_url:
            raise RuntimeError("Could not find the lyrics page link on the provided shirrim.com chords page")
        return _fetch_shirrim_lyrics(lyrics_url)

    lyrics_prefix = "\u05d4\u05de\u05d9\u05dc\u05d9\u05dd \u05e9\u05dc \u05d4\u05e9\u05d9\u05e8"
    lyrics_container = None
    
    # Strategy 1: Search for the specific lyrics container with the expected prefix
    for candidate in soup.select("div.jet-listing-dynamic-field__content"):
        text = candidate.get_text("\n", strip=True)
        if text.startswith(lyrics_prefix):
            lyrics_container = candidate
            break

    # Strategy 2: Look for "המילים של השיר:" marker and take the content after it
    if lyrics_container is None:
        for element in soup.find_all(["p", "div", "span"]):
            if "המילים של השיר:" in element.get_text():
                # The lyrics usually follow in the next few paragraphs or siblings
                # We'll try to capture the content after this marker
                lyrics_lines = []
                curr = element.find_next()
                while curr:
                    if curr.name == "h2" or curr.name == "h3": # Stop at next heading
                        break
                    text = curr.get_text("\n", strip=True)
                    if text:
                        lyrics_lines.append(text)
                    curr = curr.find_next()
                
                if lyrics_lines:
                    lyrics_text = "\n\n".join(lyrics_lines).strip()
                    if len(lyrics_text) > 50:
                        # We've effectively found the lyrics
                        title = ""
                        title_node = soup.select_one("h1.elementor-heading-title")
                        if title_node:
                            title = title_node.get_text(" ", strip=True)
                        return {
                            "lyrics": lyrics_text,
                            "title": title,
                            "source_url": normalized_url,
                        }

    # Strategy 3: Fallback to the largest block of text with many lines
    if lyrics_container is None:
        candidates = soup.select("div.jet-listing-dynamic-field__content")
        if not candidates:
            # If no jet-listing divs, search in main content
            main = soup.select_one("main") or soup.select_one("article") or soup.body
            candidates = [main] if main else []
            
        best_text = ""
        for candidate in candidates:
            text = candidate.get_text("\n", strip=True)
            if len(text) > len(best_text) and text.count("\n") > 2:
                best_text = text
        
        if len(best_text) > 50:
            # Clean up the prefix if present
            lyrics_text = best_text.replace(f"{lyrics_prefix}:", "", 1).strip()
            title = ""
            title_node = soup.select_one("h1.elementor-heading-title")
            if title_node:
                title = title_node.get_text(" ", strip=True)
            return {
                "lyrics": lyrics_text,
                "title": title,
                "source_url": normalized_url,
            }

    # Final attempt using the original logic if lyrics_container was found in strategy 1
    if lyrics_container is None:
        lyrics_url = _find_shirrim_lyrics_link(soup, normalized_url)
        if lyrics_url and lyrics_url != normalized_url:
            return _fetch_shirrim_lyrics(lyrics_url)
        raise RuntimeError("Could not locate lyrics on the provided shirrim.com page")

    extracted_lines: list[str] = []
    for paragraph in lyrics_container.find_all("p"):
        paragraph_lines = [line.strip() for line in paragraph.get_text("\n").splitlines() if line.strip()]
        if paragraph_lines:
            extracted_lines.extend(paragraph_lines)
            extracted_lines.append("")

    if extracted_lines and extracted_lines[-1] == "":
        extracted_lines.pop()

    lyrics_text = "\n".join(extracted_lines).strip()
    if not lyrics_text:
        raw_text = lyrics_container.get_text("\n", strip=True)
        lyrics_text = raw_text.replace(f"{lyrics_prefix}:", "", 1).strip()

    if not lyrics_text:
        raise RuntimeError("Lyrics block was found, but no lyrics text could be extracted")

    title = ""
    title_node = soup.select_one("h1.elementor-heading-title")
    if title_node is not None:
        title = title_node.get_text(" ", strip=True)

    return {
        "lyrics": lyrics_text,
        "title": title,
        "source_url": normalized_url,
    }


def _write_manual_project(
    paths: EditorPaths,
    audio_source: Path,
    lyrics_text: str,
    config_path: Path,
    original_name: str,
    project_name: str,
    preferred_source_name: str | None = None,
    lyrics_source_url: str = "",
) -> dict[str, Any]:
    paths.input_dir.mkdir(parents=True, exist_ok=True)
    paths.temp_dir.mkdir(parents=True, exist_ok=True)

    stored_input_name = _sanitize_filename(preferred_source_name or original_name, "manual_input.mp3")
    stored_input_path = paths.input_dir / stored_input_name
    try:
        source_path = audio_source.resolve()
    except Exception:
        source_path = audio_source
    try:
        target_path = stored_input_path.resolve()
    except Exception:
        target_path = stored_input_path

    if source_path != target_path:
        if source_path.parent == paths.input_dir.resolve():
            if stored_input_path.exists():
                stored_input_path.unlink()
            audio_source.replace(stored_input_path)
        else:
            shutil.copyfile(audio_source, stored_input_path)

    extract_and_normalize_audio(stored_input_path, paths.audio_path, config_path)
    duration_seconds = _read_wav_duration(paths.audio_path)
    lyric_lines = _clean_text_lines(lyrics_text)
    words, line_entries = _build_word_stream(lyric_lines, duration_seconds)
    intro_metadata = _project_intro_metadata(paths)

    manifest = {
        "version": 1,
        "source_audio": str(stored_input_path),
        "lines": line_entries,
        "words": words,
        "intro": intro_metadata,
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
        "lyrics_source_url": str(lyrics_source_url).strip(),
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
    manifest = {"lines": lines_payload, "words": words, "intro": _project_intro_metadata(paths)}
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
        "lyrics_source_url": "",
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "artifacts": {},
    }
    _write_json_atomic(paths.editor_manifest_path, manifest)
    _write_json_atomic(paths.overrides_path, overrides)
    _write_json_atomic(paths.state_path, state)
    _lyrics_path(paths).write_text("\n".join(lyric_lines), encoding="utf-8")
    return state


def _attach_audio_to_project(
    paths: EditorPaths,
    project_name: str,
    audio_source: Path,
    original_name: str,
    lyrics_text: str | None = None,
    preferred_source_name: str | None = None,
    lyrics_source_url: str = "",
) -> dict[str, Any]:
    effective_lyrics = lyrics_text if lyrics_text is not None else _project_lyrics_text(paths)
    return _write_manual_project(
        paths=paths,
        audio_source=audio_source,
        lyrics_text=effective_lyrics,
        config_path=paths.config_path,
        original_name=original_name,
        project_name=project_name,
        preferred_source_name=preferred_source_name,
        lyrics_source_url=lyrics_source_url,
    )


def _terminate_process_tree(pid: int) -> None:
    if pid <= 0:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                capture_output=True,
                text=True,
            )
            return
        try:
            os.killpg(pid, signal.SIGTERM)
        except Exception:
            os.kill(pid, signal.SIGTERM)
    except Exception:
        pass


def _execute_ai_first_pass_job(
    base_paths: EditorPaths,
    *,
    job_id: str,
    project_key: str,
    project_name: str,
    source_path: str,
    source_name: str,
    original_audio_name: str,
    lyrics_text: str,
    lyrics_source_url: str,
) -> dict[str, Any]:
    initial_job = _default_pipeline_job()
    initial_job.update(
        {
            "job_id": job_id,
            "status": "running",
            "started_at": _timestamp(),
            "updated_at": _timestamp(),
            "project_id": project_key,
            "project_name": project_name,
        }
    )
    _write_pipeline_job(base_paths, initial_job)
    _update_pipeline_stage(base_paths, initial_job, "download_convert", "skipped", "Using existing project audio")
    job = _read_pipeline_job(base_paths)

    def mark(stage_key: str, status: str, detail: str = "") -> None:
        nonlocal job
        job = _update_pipeline_stage(base_paths, job, stage_key, status, detail)

    try:
        mark("stem_separation", "running", "Preparing vocal stems")
        
        # Keep manual timings to seamlessly merge with the AI pass
        try:
            existing_overrides = _read_overrides(base_paths)
        except Exception:
            existing_overrides = None

        autosync_result = run_first_pass_autosync(
            Path(source_path),
            base_paths.config_path,
            project_name=project_name,
            source_name=source_name,
            original_audio_name=original_audio_name,
            lyrics_text=lyrics_text,
            lyrics_source_url=lyrics_source_url,
            project_key=project_key,
            progress_callback=mark,
            existing_overrides=existing_overrides,
        )
        _set_active_project_key(base_paths, project_key)
        completed_job = _read_pipeline_job(base_paths)
        _write_pipeline_job(
            base_paths,
            {
                **completed_job,
                "status": "completed",
                "project_id": project_key,
                "project_name": project_name,
                "current_stage_key": "",
                "current_stage_label": "",
                "stage_started_at": None,
                "stage_elapsed_seconds": 0.0,
                "updated_at": _timestamp(),
                "error": None,
                "stages": completed_job.get("stages", _default_pipeline_job()["stages"]),
            },
        )
        final_job = _decorate_pipeline_job(_read_pipeline_job(base_paths))
        return {
            "ok": True,
            "job_id": job_id,
            "status": "completed",
            "message": "Pipeline completed",
            "project_id": project_key,
            "project_name": project_name,
            "stages": final_job.get("stages", []),
            "lyrics_source_url": lyrics_source_url,
            "lyrics_title": str(autosync_result.get("transcript", {}).get("correction_model_name", "")).strip(),
        }
    except Exception as exc:
        mark("stem_separation", "error", str(exc))
        failed_job = _read_pipeline_job(base_paths)
        _write_pipeline_job(
            base_paths,
            {
                **failed_job,
                "status": "error",
                "updated_at": _timestamp(),
                "error": str(exc),
                "current_stage_key": "",
                "current_stage_label": "",
                "stage_started_at": None,
            },
        )
        raise


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
    placed_word_count = max(int(overrides.get("placed_word_count", 0) or 0), 0)
    word_overrides = overrides.get("words", {})
    if not isinstance(word_overrides, Mapping):
        word_overrides = {}

    # Identify line-ending words
    line_end_word_ids = set()
    raw_lines = manifest.get("lines", [])
    if isinstance(raw_lines, list):
        for line in raw_lines:
            if isinstance(line, Mapping):
                word_ids = line.get("word_ids", [])
                if word_ids:
                    line_end_word_ids.add(str(word_ids[-1]).strip())

    # Pass 1: Resolve starts and base ends
    all_resolved = []
    raw_words = manifest.get("words", [])
    if not isinstance(raw_words, list):
        raw_words = []

    for item in raw_words:
        if not isinstance(item, Mapping):
            continue
        word_id = str(item.get("id", "")).strip()
        if not word_id:
            continue
        word_index = max(int(item.get("index", len(all_resolved))), 0)
        
        # Only process words that have been "placed" in the UI
        if word_index >= placed_word_count:
            continue

        start = float(item.get("start", 0.0)) + global_offset
        # Vocal end MUST be based on the natural duration to detect gaps correctly.
        # We calculate it before applying overrides to avoid circular logic.
        duration = float(item.get("end", start + 0.12)) - (float(item.get("start", start)))
        duration = max(duration, 0.12)
        vocal_end = start + duration
        
        override = word_overrides.get(word_id, {})
        if isinstance(override, Mapping):
            if "offset" in override:
                offset = float(override["offset"])
                start += offset
                vocal_end += offset
            if override.get("start") is not None:
                start = float(override["start"])
            if override.get("end") is not None:
                # We specifically IGNORE override.end for the 'chained' logic threshold,
                # but we keep the variable for any existing legacy logic if needed.
                vocal_end = float(override["end"])
            if "stretch" in override:
                stretch_duration = max(vocal_end - start, 0.001) * max(float(override["stretch"]), 0.01)
                vocal_end = start + stretch_duration
        
        if vocal_end <= start:
            vocal_end = start + 0.12
            
        all_resolved.append({
            "id": word_id,
            "index": word_index,
            "start": start,
            "vocal_end": vocal_end,
            "text": str(item.get("text", "")).strip(),
            "is_line_end": word_id in line_end_word_ids
        })

    # Sort by index to ensure correct chaining
    all_resolved.sort(key=lambda x: x["index"])

    # Pass 2: Apply Chaining and Padding rules
    windows: dict[str, tuple[float, float, str]] = {}
    for i, word in enumerate(all_resolved):
        # Cap natural vocal duration at 2.0 seconds to prevent tails extending into silence
        natural_dur = word["vocal_end"] - word["start"]
        vocal_end_capped = word["start"] + min(natural_dur, 2.0)
        
        if i == len(all_resolved) - 1:
            # Rule: Last word of song gets exactly 2.0s padding
            final_end = vocal_end_capped + 2.0
        else:
            next_word = all_resolved[i+1]
            next_start = next_word["start"]
            gap = next_start - vocal_end_capped
            
            if word["is_line_end"] or gap > 2.0:
                # Rule: End of line or large gap gets exactly 2.0s padding (relative to capped vocal end)
                final_end = min(vocal_end_capped + 2.0, next_start)
            else:
                # Rule: Chained presentation
                final_end = next_start
        
        # Final safety check
        if final_end <= word["start"]:
            final_end = word["start"] + 0.01
            
        windows[word["id"]] = (
            round(word["start"], 3), 
            round(final_end, 3), 
            word["text"]
        )

    return windows


def _build_manual_subtitles(paths: EditorPaths, config: Mapping[str, Any]) -> Path:
    manifest = _read_manifest(paths)
    overrides = _read_overrides(paths)
    settings = build_subtitle_settings(config)
    temp_dir = paths.temp_dir
    temp_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = temp_dir / settings["assets_dir_name"]
    settings["assets_dir"] = str(assets_dir)
    preroll_seconds = max(float(settings.get("sentence_preroll_seconds", 1.0)), 0.0)

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
    previous_end = -1.0
    previous_promote = -1.0
    for index, payload in enumerate(line_payloads):
        next_start = None
        if index + 1 < len(line_payloads):
            next_start = float(line_payloads[index + 1]["start"])

        start = float(payload["start"])
        end = float(payload["end"])
        if next_start is not None:
            next_visible_start = max(next_start - preroll_seconds, 0.0)
            end = min(end, max(start + overlap_epsilon, next_visible_start - overlap_epsilon))

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
        
        t_promote = max(previous_end + 0.2, start - 0.5) if previous_end >= 0 else max(0.0, start - 0.5)
        t_promote = min(t_promote, start)
        t_reveal = previous_promote if previous_promote >= 0 else max(0.0, t_promote - 1.5)
        t_reveal = min(t_reveal, t_promote - 0.1)
        
        previous_end = end
        previous_promote = t_promote
        
        dialogue_lines.append(_ass_placeholder_line(start, end, display_text))
        image_events.extend(
            _build_image_events(
                str(payload["id"]),
                display_text,
                start,
                end,
                windows,
                t_reveal,
                t_promote,
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

    subtitles_dir = temp_dir / "subtitles"
    subtitles_dir.mkdir(parents=True, exist_ok=True)
    ass_path = subtitles_dir / settings["output_ass_name"]
    manifest_path = paths.manifest_path
    ass_path.write_text(build_ass_header(settings) + "\n".join(dialogue_lines) + "\n", encoding="utf-8")
    _write_json_atomic(
        manifest_path,
            {
                "intro": {
                    "title": _project_intro_metadata(paths).get("intro_title", ""),
                    "subtitle": _project_intro_metadata(paths).get("intro_subtitle", ""),
                    "intro_duration_seconds": _project_intro_metadata(paths).get("intro_duration_seconds", 2.5),
                },
                "lines": line_entries,
                "events": _append_countdown_events(image_events, line_entries, assets_dir, settings),
        }
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
    placed_word_count = max(int(overrides.get("placed_word_count", 0) or 0), 0)

    resolved_words: list[dict[str, Any]] = []
    for index, item in enumerate(raw_words):
        if not isinstance(item, Mapping):
            continue
        word_index = max(int(item.get("index", index)), 0)
        if word_index >= placed_word_count:
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
                "index": word_index,
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
            if not no_vocals_path.is_absolute():
                no_vocals_path = (paths.root_dir / no_vocals_path).resolve()
            if no_vocals_path.exists():
                return no_vocals_path

    no_vocals_fallback = paths.audio_path.with_name("no_vocals.wav")
    if no_vocals_fallback.exists():
        return no_vocals_fallback
    return paths.audio_path


def _artifact_audio_path(paths: EditorPaths, artifact_name: str) -> Path:
    safe_name = str(artifact_name or "").strip()
    if safe_name not in {"audio_wav", "vocals_wav", "no_vocals_wav"}:
        safe_name = "audio_wav"

    state = _state_payload(paths)
    artifacts = state.get("artifacts", {})
    if isinstance(artifacts, Mapping):
        candidate = artifacts.get(safe_name)
        if candidate:
            candidate_path = Path(str(candidate))
            if not candidate_path.is_absolute():
                candidate_path = (paths.root_dir / candidate_path).resolve()
            if candidate_path.exists():
                return candidate_path

    if safe_name == "audio_wav":
        return paths.audio_path

    fallback = paths.audio_path.with_name(safe_name.replace("_wav", "") + ".wav")
    if fallback.exists():
        return fallback
    return paths.audio_path


def _ensure_project_backing_track(paths: EditorPaths) -> Path:
    LOGGER.info("_ensure_project_backing_track started for project: %s", paths.state_path)
    preferred_audio = _manual_render_audio_path(paths)
    if preferred_audio.exists() and preferred_audio != paths.audio_path and preferred_audio.parent.resolve() == paths.audio_path.parent.resolve():
        LOGGER.info("_ensure_project_backing_track: using preferred audio %s", preferred_audio)
        return preferred_audio

    if not paths.audio_path.exists():
        LOGGER.error("_ensure_project_backing_track: audio path does not exist: %s", paths.audio_path)
        return paths.audio_path

    try:
        LOGGER.info("_ensure_project_backing_track: calling separate_vocals for %s", paths.audio_path)
        separation_outputs = separate_vocals(paths.audio_path, paths.config_path)
        LOGGER.info("_ensure_project_backing_track: separation completed. outputs: %s", separation_outputs)
    except Exception as exc:
        LOGGER.exception("_ensure_project_backing_track: separation failed: %s", exc)
        return paths.audio_path

    project_vocals_path = paths.audio_path.with_name("vocals.wav")
    project_no_vocals_path = paths.audio_path.with_name("no_vocals.wav")
    source_vocals_path = Path(str(separation_outputs.get("vocals", project_vocals_path)))
    source_no_vocals_path = Path(str(separation_outputs.get("no_vocals", project_no_vocals_path)))
    
    LOGGER.info("_ensure_project_backing_track: copying stems from %s to %s", source_vocals_path, project_vocals_path)
    if source_vocals_path.exists() and source_vocals_path.resolve() != project_vocals_path.resolve():
        shutil.copyfile(source_vocals_path, project_vocals_path)
    if source_no_vocals_path.exists() and source_no_vocals_path.resolve() != project_no_vocals_path.resolve():
        shutil.copyfile(source_no_vocals_path, project_no_vocals_path)

    state = _state_payload(paths)
    existing_artifacts = state.get("artifacts", {})
    artifacts = dict(existing_artifacts) if isinstance(existing_artifacts, Mapping) else {}
    artifacts["audio_wav"] = str(paths.audio_path)
    artifacts["vocals_wav"] = str(project_vocals_path if project_vocals_path.exists() else source_vocals_path)
    artifacts["no_vocals_wav"] = str(project_no_vocals_path if project_no_vocals_path.exists() else source_no_vocals_path)
    state["artifacts"] = artifacts
    _write_json_atomic(paths.state_path, state)
    LOGGER.info("_ensure_project_backing_track: state updated and stems ready")

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
    ass_path = paths.temp_dir / "subtitles" / settings["output_ass_name"]
    ass_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path = paths.manifest_path
    ass_path.write_text(build_ass_header(settings) + "\n".join(dialogue_lines) + "\n", encoding="utf-8")
    _write_json_atomic(
        manifest_path,
        {"lines": line_entries, "events": image_events}
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
        base_name = _clean_media_display_name(source_name) or Path(source_name).stem.strip() or "karaoke"
        return f"{base_name} (Kareoke).mp4"

    original_audio_name = str(state.get("original_audio_name", "")).strip()
    if original_audio_name:
        base_name = _clean_media_display_name(original_audio_name) or Path(original_audio_name).stem.strip() or "karaoke"
        return f"{base_name} (Kareoke).mp4"

    audio_source_raw = str(state.get("audio_source", "")).strip()
    if audio_source_raw:
        base_name = _clean_media_display_name(audio_source_raw) or Path(audio_source_raw).stem.strip() or "karaoke"
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
    app.config["PIPELINE_LOCK"] = threading.Lock()
    app.config["PIPELINE_JOB"] = _read_pipeline_job(base_paths)
    app.config["PIPELINE_WORKER"] = None
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

    def set_pipeline_job(**updates: Any) -> dict[str, Any]:
        with app.config["PIPELINE_LOCK"]:
            job = dict(app.config["PIPELINE_JOB"])
            if not job:
                job = _default_pipeline_job()
            job.update(updates)
            if "stages" not in job or not isinstance(job.get("stages"), list):
                job["stages"] = _default_pipeline_job()["stages"]
            app.config["PIPELINE_JOB"] = job
            _write_pipeline_job(base_paths, job)
            return job

    def _current_pipeline_worker() -> dict[str, Any] | None:
        worker = app.config.get("PIPELINE_WORKER")
        if not isinstance(worker, dict) or not worker:
            return None
        process = worker.get("process")
        if process is not None and hasattr(process, "is_alive") and not process.is_alive():
            app.config["PIPELINE_WORKER"] = None
            return None
        return worker

    def _clear_pipeline_worker() -> None:
        app.config["PIPELINE_WORKER"] = None

    def _launch_ai_first_pass_worker(
        job_id: str,
        *,
        project_key: str,
        project_name: str,
        source_path: Path,
        source_name: str,
        original_audio_name: str,
        lyrics_text: str,
        lyrics_source_url: str,
    ) -> mp.Process:
        worker = mp.get_context("spawn").Process(
            target=_execute_ai_first_pass_job,
            args=(
                base_paths,
            ),
            kwargs={
                "job_id": job_id,
                "project_key": project_key,
                "project_name": project_name,
                "source_path": str(source_path),
                "source_name": source_name,
                "original_audio_name": original_audio_name,
                "lyrics_text": lyrics_text,
                "lyrics_source_url": lyrics_source_url,
            },
            daemon=True,
        )
        worker.start()
        app.config["PIPELINE_WORKER"] = {
            "kind": "ai_first_pass",
            "job_id": job_id,
            "process": worker,
            "project_key": project_key,
            "project_name": project_name,
        }
        return worker

    def _setup_audio_project(raw_payload: Mapping[str, Any], job_id: str) -> dict[str, Any]:
        """Synchronously perform normalization and lyrics fetch for an audio import."""
        audio_file_path = Path(raw_payload.get("audio_file_path", "")).resolve()
        if not audio_file_path.exists():
            raise FileNotFoundError(f"Audio file not found: {audio_file_path}")

        original_audio_name = audio_file_path.name
        file_stem = audio_file_path.stem.strip()
        project_name = file_stem or "Project"
        project_key = _project_storage_key(project_name, "project")
        project_paths = _project_paths(base_paths, project_key)

        initial_job = _default_pipeline_job()
        initial_job.update(
            {
                "job_id": job_id,
                "status": "running",
                "started_at": _timestamp(),
                "updated_at": _timestamp(),
                "project_id": project_key,
                "project_name": project_name,
            }
        )
        set_pipeline_job(**initial_job)
        
        # 1. Normalize Audio (Crucial for waveform to load immediately)
        _update_pipeline_stage(base_paths, initial_job, "lyrics_fetch", "running", "Normalizing audio and fetching lyrics")
        
        # We must normalize now so that audio.wav exists before returning 202
        extract_and_normalize_audio(audio_file_path, project_paths.audio_path, project_paths.config_path)
        
        # 2. Lyrics Fetch & Project Creation
        details = _discover_project_details_from_audio_filename(original_audio_name)
        lyrics_text = str(details.get("lyrics_text", "")).strip()
        lyrics_source_url = str(details.get("lyrics_source_url", "")).strip()

        state = _write_manual_project(
            paths=project_paths,
            audio_source=audio_file_path,
            lyrics_text=lyrics_text,
            config_path=project_paths.config_path,
            original_name=original_audio_name,
            project_name=project_name,
            lyrics_source_url=lyrics_source_url,
        )
        _update_pipeline_stage(base_paths, initial_job, "lyrics_fetch", "done", "Lyrics fetched and project created")
        _set_active_project_key(base_paths, project_key)
        
        return {
            "project_id": project_key,
            "project_name": project_name,
            "lyrics_source_url": lyrics_source_url,
            "lyrics_title": str(details.get("lyrics_title", "")).strip(),
            "lyrics_found": bool(lyrics_text),
        }

    def _run_audio_import_job(raw_payload: Mapping[str, Any], job_id: str) -> dict[str, Any]:
        """Worker job that handles the slow parts of audio import (stem separation)."""
        job = _read_pipeline_job(base_paths)
        project_key = job.get("project_id")
        project_name = job.get("project_name")
        if not project_key:
            raise RuntimeError("Project ID missing from pipeline job state")
        
        project_paths = _project_paths(base_paths, project_key)

        def mark(stage_key: str, status: str, detail: str = "") -> None:
            nonlocal job
            job = _update_pipeline_stage(base_paths, job, stage_key, status, detail)

        try:
            mark("stem_separation", "running", "Preparing vocal stems")
            try:
                _ensure_project_backing_track(project_paths)
            except Exception:
                pass
            mark("stem_separation", "done", "Vocal stems ready")
            
            # Use "completed" instead of "idle" to trigger the frontend's auto-refresh logic
            set_pipeline_job(
                status="completed",
                project_id=project_key,
                project_name=project_name,
                current_stage_key="",
                current_stage_label="",
                stage_started_at=None,
                stage_elapsed_seconds=0.0,
                updated_at=_timestamp(),
                error=None,
                stages=_read_pipeline_job(base_paths).get("stages", _default_pipeline_job()["stages"]),
            )
            final_job = _decorate_pipeline_job(_read_pipeline_job(base_paths))
            return {
                "ok": True,
                "job_id": job_id,
                "status": "completed",
                "message": "Pipeline completed",
                "project_id": project_key,
                "project_name": project_name,
                "stages": final_job.get("stages", []),
            }
        except Exception as exc:
            mark("stem_separation", "error", str(exc))
            set_pipeline_job(
                status="error",
                updated_at=_timestamp(),
                error=str(exc),
                current_stage_key="",
                current_stage_label="",
                stage_started_at=None,
            )
            raise



    def start_audio_import_job(raw_payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        audio_file_path = str(raw_payload.get("audio_file_path", "")).strip()
        if not audio_file_path:
            return 400, {"error": "audio_file_path is required"}

        # Pre-calculate project info so the frontend can switch projects immediately
        original_audio_name = Path(audio_file_path).name
        file_stem = Path(original_audio_name).stem.strip()
        project_name = file_stem or "Project"
        project_key = _project_storage_key(project_name, "project")

        current_job = _decorate_pipeline_job(_read_pipeline_job(base_paths))
        if str(current_job.get("status", "")).lower() == "running":
            return 409, {"error": "Another pipeline job is already running"}

        job_id = uuid.uuid4().hex
        try:
            # Synchronously setup the project (Normalization + Lyrics)
            # This ensures audio.wav and project state exist before returning 202
            setup_info = _setup_audio_project(raw_payload, job_id)
        except Exception as exc:
            return 500, {"ok": False, "job_id": job_id, "error": str(exc)}

        if app.testing:
            try:
                return 200, _run_audio_import_job(raw_payload, job_id)
            except ValueError as exc:
                return 400, {"error": str(exc), "job_id": job_id}
            except RuntimeError as exc:
                return 409, {"error": str(exc), "job_id": job_id}
            except Exception as exc:
                return 500, {"ok": False, "job_id": job_id, "error": str(exc)}

        def worker() -> None:
            try:
                LOGGER.info("Background worker starting audio import job: %s", job_id)
                _run_audio_import_job(raw_payload, job_id)
                LOGGER.info("Background worker completed audio import job: %s", job_id)
            except Exception as exc:
                LOGGER.exception("Background worker failed audio import job %s: %s", job_id, exc)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        return 202, {
            "ok": True, 
            "job_id": job_id, 
            "status": "running", 
            "message": "Audio import started",
            "project_id": project_key,
            "project_name": project_name
        }

    def _setup_youtube_project(raw_payload: Mapping[str, Any], job_id: str) -> dict[str, Any]:
        """Synchronously perform download, normalization, and lyrics fetch for a YouTube import."""
        youtube_url = str(raw_payload.get("youtube_url", "")).strip()
        if not youtube_url:
            raise ValueError("youtube_url is required")

        requested_project_id = str(raw_payload.get("project_id", "")).strip()
        requested_project_name = str(raw_payload.get("project_name", "")).strip()
        requested_lyrics_text_raw = raw_payload.get("lyrics_text")
        requested_lyrics_text = str(requested_lyrics_text_raw).strip() if requested_lyrics_text_raw is not None else None

        # 1. Download & Convert
        # We do this first to discover the actual project name/ID from YouTube metadata
        downloaded_audio = _download_youtube_audio(base_paths, youtube_url)
        original_name = downloaded_audio.name
        
        discovered_details: dict[str, str] = {}
        try:
            discovered_details = _discover_project_details_from_youtube(youtube_url, original_name)
        except Exception:
            discovered_details = {}
        
        resolved_lyrics_text = requested_lyrics_text or str(discovered_details.get("lyrics_text", "")).strip()
        resolved_project_name = (
            str(discovered_details.get("youtube_project_name", "")).strip()
            or requested_project_name
            or _hebrew_dash_title(original_name)
            or _clean_media_display_name(original_name)
            or Path(original_name).stem
        )
        project_key = requested_project_id or _project_storage_key(resolved_project_name, "project")
        
        preferred_source_name = _preferred_source_audio_name(resolved_project_name, original_name)
        cleaned_audio_path = downloaded_audio.with_name(preferred_source_name)
        try:
            if downloaded_audio.resolve() != cleaned_audio_path.resolve():
                cleaned_audio_path.parent.mkdir(parents=True, exist_ok=True)
                if cleaned_audio_path.exists():
                    cleaned_audio_path.unlink()
                downloaded_audio.replace(cleaned_audio_path)
                downloaded_audio = cleaned_audio_path
        except Exception:
            pass
            
        # Now that we have the resolved project info, we can initialize the pipeline job
        initial_job = _default_pipeline_job()
        initial_job.update(
            {
                "job_id": job_id,
                "status": "running",
                "started_at": _timestamp(),
                "updated_at": _timestamp(),
                "project_id": project_key,
                "project_name": resolved_project_name,
            }
        )
        set_pipeline_job(**initial_job)
        
        _update_pipeline_stage(base_paths, initial_job, "download_convert", "done", f"Downloaded {cleaned_audio_path.name}")
        
        # 2. Lyrics Fetch & Project Creation
        project_paths = _project_paths(base_paths, project_key)
        
        _update_pipeline_stage(base_paths, initial_job, "lyrics_fetch", "running", "Fetching lyrics and creating project")
        state = _attach_audio_to_project(
            paths=project_paths,
            project_name=resolved_project_name,
            audio_source=downloaded_audio,
            original_name=original_name,
            lyrics_text=resolved_lyrics_text,
            preferred_source_name=preferred_source_name,
            lyrics_source_url=str(discovered_details.get("lyrics_source_url", "")).strip(),
        )
        _update_pipeline_stage(base_paths, initial_job, "lyrics_fetch", "done", "Lyrics fetched and project created")
        _set_active_project_key(base_paths, project_key)
        
        return {
            "project_id": project_key,
            "project_name": resolved_project_name,
            "lyrics_source_url": str(discovered_details.get("lyrics_source_url", "")).strip(),
            "lyrics_title": str(discovered_details.get("lyrics_title", "")).strip(),
            "lyrics_found": bool(resolved_lyrics_text),
        }

    def _run_youtube_import_job(raw_payload: Mapping[str, Any], job_id: str) -> dict[str, Any]:
        """Worker job that handles the slow parts of YouTube import (stem separation)."""
        # We assume _setup_youtube_project has already run synchronously.
        # We just need to retrieve the current project state.
        job = _read_pipeline_job(base_paths)
        project_key = str(job.get("project_id", "")).strip()
        project_name = str(job.get("project_name", "")).strip()
        if not project_key:
            raise RuntimeError("Project ID missing from pipeline job state")
        
        project_paths = _project_paths(base_paths, project_key)

        def mark(stage_key: str, status: str, detail: str = "") -> None:
            nonlocal job
            job = _update_pipeline_stage(base_paths, job, stage_key, status, detail)

        try:
            mark("stem_separation", "running", "Running stem separation")
            try:
                _ensure_project_backing_track(project_paths)
            except Exception:
                pass
            mark("stem_separation", "done", "Editor project ready")
            
            # Use "completed" instead of "idle" to trigger the frontend's auto-refresh logic
            set_pipeline_job(
                status="completed",
                project_id=project_key,
                project_name=project_name,
                current_stage_key="",
                current_stage_label="",
                stage_started_at=None,
                stage_elapsed_seconds=0.0,
                updated_at=_timestamp(),
                error=None,
                stages=_read_pipeline_job(base_paths).get("stages", _default_pipeline_job()["stages"]),
            )
            final_job = _decorate_pipeline_job(_read_pipeline_job(base_paths))
            return {
                "ok": True,
                "job_id": job_id,
                "status": "completed",
                "message": "Pipeline completed",
                "project_id": project_key,
                "project_name": project_name,
                "stages": final_job.get("stages", []),
            }

        except Exception as exc:
            mark("stem_separation", "error", str(exc))
            set_pipeline_job(
                status="error",
                updated_at=_timestamp(),
                error=str(exc),
                current_stage_key="",
                current_stage_label="",
                stage_started_at=None,
            )
            raise


    def start_youtube_import_job(raw_payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        youtube_url = str(raw_payload.get("youtube_url", "")).strip()
        if not youtube_url:
            return 400, {"error": "youtube_url is required"}

        current_job = _decorate_pipeline_job(_read_pipeline_job(base_paths))
        if str(current_job.get("status", "")).lower() == "running":
            app.config["PIPELINE_JOB"] = current_job
            return 409, {"error": "Another pipeline job is already running"}

        job_id = uuid.uuid4().hex
        
        try:
            # Synchronously setup the project (Download, Lyrics, Normalization)
            # This ensures waveform and lyrics are present immediately.
            setup_info = _setup_youtube_project(raw_payload, job_id)
        except Exception as exc:
            return 500, {"ok": False, "job_id": job_id, "error": str(exc)}

        def worker() -> None:
            try:
                _run_youtube_import_job(raw_payload, job_id)
            except Exception:
                pass

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        
        return 200, {
            "ok": True, 
            "job_id": job_id, 
            "status": "running", 
            "message": "Project created. Stems are processing in background.",
            "project_id": setup_info["project_id"],
            "project_name": setup_info["project_name"],
            "lyrics_source_url": setup_info["lyrics_source_url"],
            "lyrics_title": setup_info["lyrics_title"],
            "lyrics_found": setup_info["lyrics_found"],
        }


    def start_ai_first_pass_job() -> tuple[int, dict[str, Any]]:
        project_key = _active_project_key(base_paths) or LEGACY_PROJECT_KEY
        if not project_key:
            return 400, {"error": "No project is loaded"}

        worker = _current_pipeline_worker()
        if worker is not None and worker.get("kind") == "ai_first_pass":
            process = worker.get("process")
            if process is not None and hasattr(process, "is_alive") and process.is_alive():
                current_job = _decorate_pipeline_job(_read_pipeline_job(base_paths))
                app.config["PIPELINE_JOB"] = current_job
                return 409, {"error": "Another pipeline job is already running"}

        current_job = _decorate_pipeline_job(_read_pipeline_job(base_paths))
        if str(current_job.get("status", "")).lower() == "running":
            app.config["PIPELINE_JOB"] = current_job
            return 409, {"error": "Another pipeline job is already running"}

        project_paths = _project_paths(base_paths, project_key)
        state = _state_payload(project_paths)
        source_name = _guess_input_audio_name(project_paths, state)
        source_path = project_paths.input_dir / source_name if source_name else project_paths.audio_path
        if not source_path.exists():
            return 404, {"error": f"Audio file not found: {source_path}"}

        project_name = _project_display_name(project_paths, project_key)
        lyrics_text = _project_lyrics_text(project_paths)
        lyrics_source_url = str(state.get("lyrics_source_url", "")).strip()
        original_audio_name = str(state.get("original_audio_name", "")).strip() or source_name
        job_id = uuid.uuid4().hex
        try:
            if app.testing:
                payload = _execute_ai_first_pass_job(
                    base_paths,
                    job_id=job_id,
                    project_key=project_key,
                    project_name=project_name,
                    source_path=str(source_path),
                    source_name=source_name or source_path.name,
                    original_audio_name=original_audio_name or source_path.name,
                    lyrics_text=lyrics_text,
                    lyrics_source_url=lyrics_source_url,
                )
                app.config["PIPELINE_JOB"] = _decorate_pipeline_job(_read_pipeline_job(base_paths))
                _clear_pipeline_worker()
                return 200, payload
        except ValueError as exc:
            return 400, {"error": str(exc), "job_id": job_id}
        except RuntimeError as exc:
            return 409, {"error": str(exc), "job_id": job_id}
        except Exception as exc:
            return 500, {"ok": False, "job_id": job_id, "error": str(exc)}

        initial_job = _default_pipeline_job()
        initial_job.update(
            {
                "job_id": job_id,
                "status": "running",
                "started_at": _timestamp(),
                "updated_at": _timestamp(),
                "project_id": project_key,
                "project_name": project_name,
            }
        )
        set_pipeline_job(**initial_job)
        _launch_ai_first_pass_worker(
            job_id,
            project_key=project_key,
            project_name=project_name,
            source_path=source_path,
            source_name=source_name,
            original_audio_name=original_audio_name,
            lyrics_text=lyrics_text,
            lyrics_source_url=lyrics_source_url,
        )
        return 202, {"ok": True, "job_id": job_id, "status": "running", "message": "Pipeline started"}

    @app.get("/")
    @app.get("/ui/timing_editor.html")
    def editor_index() -> Response:
        if not base_paths.ui_path.exists():
            return _json_response({"error": f"UI file not found: {base_paths.ui_path}"}, status=404)
        return send_file(base_paths.ui_path)

    @app.get("/api/manifest")
    def api_manifest() -> Response:
        project_paths = current_paths()
        _ensure_manifest_consistency(project_paths)
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
        artifact_name = request.args.get("artifact", default="audio_wav", type=str)
        audio_path = _artifact_audio_path(project_paths, artifact_name)
        if not audio_path.exists():
            return _json_response({"error": f"Audio file not found: {audio_path}"}, status=404)
        return send_file(audio_path, mimetype="audio/wav", conditional=True)

    @app.get("/api/waveform")
    def api_waveform() -> Response:
        project_paths = current_paths()
        waveform_path = _artifact_audio_path(project_paths, "vocals_wav")
        if not waveform_path.exists():
            return _json_response({"error": f"Audio file not found: {waveform_path}"}, status=404)
        bins = request.args.get("bins", default=1400, type=int)
        bins = max(100, min(bins, 4000))
        return _json_response(_waveform_payload(waveform_path, bins))

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

    @app.post("/api/projects/save")
    def api_projects_save() -> Response:
        raw_payload = request.get_json(silent=True)
        if not isinstance(raw_payload, Mapping):
            return _json_response({"error": "Request body must be a JSON object"}, status=400)

        current_project_id = str(raw_payload.get("project_id", "")).strip() or (_active_project_key(base_paths) or LEGACY_PROJECT_KEY)
        if not current_project_id:
            return _json_response({"error": "No project is loaded"}, status=400)

        project_name = str(raw_payload.get("project_name", "")).strip()
        project_paths = _project_paths(base_paths, current_project_id)
        if not project_name:
            project_name = _project_display_name(project_paths, current_project_id)

        try:
            overrides_payload = _sanitize_override_body(raw_payload)
        except ValueError as exc:
            return _json_response({"error": str(exc)}, status=400)

        try:
            if current_project_id != LEGACY_PROJECT_KEY:
                project_id, project_paths = _rename_project(base_paths, current_project_id, project_name)
            else:
                project_id = current_project_id
                _update_project_state_name(project_paths, project_name)
            _write_json_atomic(project_paths.overrides_path, overrides_payload)
            if "lyrics_text" in raw_payload:
                _save_project_lyrics(project_paths, str(raw_payload.get("lyrics_text", "")).strip())
        except (FileNotFoundError, ValueError) as exc:
            return _json_response({"error": str(exc)}, status=400)

        _set_active_project_key(base_paths, project_id)
        return _json_response(
            {
                "ok": True,
                "project_id": project_id,
                "project_name": _project_display_name(project_paths, project_id),
                "overrides": overrides_payload,
            }
        )

    @app.post("/api/projects/delete")
    def api_projects_delete() -> Response:
        raw_payload = request.get_json(silent=True)
        if not isinstance(raw_payload, Mapping):
            return _json_response({"error": "Request body must be a JSON object"}, status=400)

        project_id = str(raw_payload.get("project_id", "")).strip() or (_active_project_key(base_paths) or "")
        try:
            next_project_id = _delete_project(base_paths, project_id)
        except (FileNotFoundError, ValueError) as exc:
            return _json_response({"error": str(exc)}, status=400)

        next_project_paths = _project_paths(base_paths, next_project_id) if next_project_id else base_paths
        return _json_response(
            {
                "ok": True,
                "deleted_project_id": project_id,
                "current_project_id": next_project_id,
                "current_project_name": _project_display_name(next_project_paths, next_project_id) if next_project_id else "",
            }
        )

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

    @app.post("/api/projects/attach-audio")
    def api_projects_attach_audio() -> Response:
        project_id = str(request.form.get("project_id", "")).strip()
        if not project_id:
            return _json_response({"error": "project_id is required"}, status=400)

        available = {project["id"]: project for project in _list_projects(base_paths)}
        if project_id not in available:
            return _json_response({"error": f"Unknown project: {project_id}"}, status=404)

        audio_file = request.files.get("audio_file")
        if audio_file is None or not audio_file.filename:
            return _json_response({"error": "audio_file is required"}, status=400)

        lyrics_text_raw = request.form.get("lyrics_text")
        lyrics_text = str(lyrics_text_raw).strip() if lyrics_text_raw is not None else None
        lyrics_file = request.files.get("lyrics_file")
        if lyrics_file is not None and lyrics_file.filename:
            lyrics_text = lyrics_file.read().decode("utf-8", errors="replace").strip()

        project_paths = _project_paths(base_paths, project_id)
        source_name = _sanitize_filename(audio_file.filename, "manual_input.mp3")
        source_path = project_paths.input_dir / source_name
        source_path.parent.mkdir(parents=True, exist_ok=True)
        audio_file.save(str(source_path))

        state = _attach_audio_to_project(
            paths=project_paths,
            project_name=_project_display_name(project_paths, project_id),
            audio_source=source_path,
            original_name=audio_file.filename,
            lyrics_text=lyrics_text,
        )
        _set_active_project_key(base_paths, project_id)
        return _json_response(
            {
                "ok": True,
                "project_id": project_id,
                "project_name": _project_display_name(project_paths, project_id),
                "state": state,
                "manifest_path": str(project_paths.editor_manifest_path),
                "audio_path": str(project_paths.audio_path),
                "overrides_path": str(project_paths.overrides_path),
            }
        )

    @app.get("/api/session")
    def api_session() -> Response:
        project_key = _active_project_key(base_paths) or LEGACY_PROJECT_KEY
        project_paths = _project_paths(base_paths, project_key)
        _ensure_manifest_consistency(project_paths)
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
            "has_audio": project_paths.audio_path.exists(),
            "original_audio_name": str(state.get("original_audio_name", "")).strip(),
            "lyrics_text": lyrics_text,
            "lyrics_source_url": str(state.get("lyrics_source_url", "")).strip(),
            "output_video_name": _manual_output_video_name(project_paths),
            "output_video_path": str(_default_output_video_path(project_paths)),
        }
        return _json_response(payload)

    @app.post("/api/import/youtube")
    def api_import_youtube() -> Response:
        raw_payload = request.get_json(silent=True) or {}
        if not isinstance(raw_payload, Mapping):
            return _json_response({"error": "Invalid JSON payload"}, status=400)
        status_code, payload = start_youtube_import_job(raw_payload)
        return _json_response(payload, status=status_code)

    @app.post("/api/pipeline/first-pass")
    def api_pipeline_first_pass() -> Response:
        status_code, payload = start_ai_first_pass_job()
        return _json_response(payload, status=status_code)

    @app.post("/api/pipeline/stop")
    def api_pipeline_stop() -> Response:
        worker = _current_pipeline_worker()
        stopped = False
        pid = None
        if worker is not None:
            process = worker.get("process")
            if process is not None and hasattr(process, "pid"):
                pid = int(getattr(process, "pid") or 0)
            if process is not None and hasattr(process, "is_alive") and process.is_alive():
                stopped = True
                if pid:
                    _terminate_process_tree(pid)
                try:
                    process.join(timeout=2.0)
                except Exception:
                    pass
                if hasattr(process, "close"):
                    try:
                        process.close()
                    except Exception:
                        pass
        _clear_pipeline_worker()
        set_pipeline_job(**_default_pipeline_job())
        return _json_response({"ok": True, "status": "idle", "stopped": stopped, "pid": pid})

    @app.post("/api/lyrics/import")
    def api_import_lyrics() -> Response:
        raw_payload = request.get_json(silent=True) or {}
        if not isinstance(raw_payload, Mapping):
            return _json_response({"error": "Invalid JSON payload"}, status=400)

        lyrics_url = str(raw_payload.get("lyrics_url", "")).strip()
        if not lyrics_url:
            return _json_response({"error": "lyrics_url is required"}, status=400)

        try:
            payload = _fetch_shirrim_lyrics(lyrics_url)
        except ValueError as exc:
            return _json_response({"error": str(exc)}, status=400)
        except Exception as exc:
            return _json_response({"error": str(exc)}, status=502)

        return _json_response({"ok": True, **payload})

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

    @app.post("/api/import/audio-auto")
    def api_import_audio_auto() -> Response:
        audio_file = request.files.get("audio_file")
        if audio_file is None or not audio_file.filename:
            return _json_response({"error": "audio_file is required"}, status=400)

        original_audio_name = audio_file.filename
        source_name = _sanitize_filename(original_audio_name, "manual_input.mp3")
        
        # We need a project key to save the file into the correct directory before launching the job
        file_stem = Path(original_audio_name).stem.strip()
        project_name = file_stem or "Project"
        project_key = _project_storage_key(project_name, "project")
        project_paths = _project_paths(base_paths, project_key)
        
        source_path = project_paths.input_dir / source_name
        source_path.parent.mkdir(parents=True, exist_ok=True)
        audio_file.save(str(source_path))

        status_code, payload = start_audio_import_job({"audio_file_path": str(source_path)})
        return _json_response(payload, status=status_code)

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

    @app.get("/api/pipeline/status")
    def api_pipeline_status() -> Response:
        _current_pipeline_worker()
        job = _decorate_pipeline_job(_read_pipeline_job(base_paths))
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
