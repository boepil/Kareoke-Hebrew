"""Materialize editor-ready projects from aligned transcripts.

The timing editor expects a manifest/override pair where the current word count
matches the lyrics text and `placed_word_count` reflects how many words are
already committed. This helper writes that structure directly so the editor
can open with a first-pass sync already in place.
"""

from __future__ import annotations

import json
import re
import shutil
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml


def load_config(config: str | Path | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(config, Mapping):
        return dict(config)

    config_path = Path(config)
    with config_path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Config file must contain a mapping: {config_path}")
    return loaded


def _paths_section(config: Mapping[str, Any]) -> Mapping[str, Any]:
    paths = config.get("paths")
    if not isinstance(paths, Mapping):
        raise KeyError("Missing 'paths' section in config")
    return paths


def _subtitle_section(config: Mapping[str, Any]) -> Mapping[str, Any]:
    section = config.get("subtitle_builder")
    if not isinstance(section, Mapping):
        raise KeyError("Missing 'subtitle_builder' section in config")
    return section


def _sanitize_filename(name: str, fallback: str) -> str:
    cleaned = Path(name).name.strip()
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or fallback


def _project_storage_key(name: str, fallback: str = "project") -> str:
    cleaned = _sanitize_filename(name, fallback)
    return cleaned.replace(".", "_")


def _clean_lines(raw_text: str) -> list[str]:
    lines = [re.sub(r"\s+", " ", line).strip() for line in str(raw_text or "").splitlines()]
    return [line for line in lines if line]


def _split_words(text: str) -> list[str]:
    return [token for token in re.split(r"\s+", text.strip()) if token]


def _read_wav_duration(audio_path: Path) -> float:
    if not audio_path.exists():
        return 0.0
    with wave.open(str(audio_path), "rb") as wav_file:
        frame_rate = wav_file.getframerate()
        if frame_rate <= 0:
            return 0.0
        total_frames = wav_file.getnframes()
    return round(total_frames / frame_rate, 6)


def _flatten_aligned_words(aligned_transcript: Mapping[str, Any]) -> list[dict[str, Any]]:
    segments = aligned_transcript.get("segments", [])
    if not isinstance(segments, list):
        return []

    flattened: list[dict[str, Any]] = []
    for segment_index, segment in enumerate(segments):
        if not isinstance(segment, Mapping):
            continue
        words = segment.get("words", [])
        if isinstance(words, list) and words:
            for word in words:
                if not isinstance(word, Mapping):
                    continue
                token = re.sub(r"\s+", " ", str(word.get("word", "")).strip())
                start = word.get("start")
                end = word.get("end")
                if not token or start is None or end is None:
                    continue
                flattened.append(
                    {
                        "text": token,
                        "start": float(start),
                        "end": float(end),
                        "segment_index": segment_index,
                    }
                )
            continue

        text = re.sub(r"\s+", " ", str(segment.get("text", "")).strip())
        start = segment.get("start")
        end = segment.get("end")
        if not text or start is None or end is None:
            continue
        words_fallback = _split_words(text)
        if not words_fallback:
            continue
        segment_start = float(start)
        segment_end = float(end)
        duration = max(segment_end - segment_start, 0.01)
        slot = duration / len(words_fallback)
        for word_index, token in enumerate(words_fallback):
            word_start = segment_start + word_index * slot
            word_end = segment_start + (word_index + 1) * slot
            flattened.append(
                {
                    "text": token,
                    "start": float(word_start),
                    "end": float(word_end),
                    "segment_index": segment_index,
                }
            )
    return flattened


def _build_word_stream(
    lyrics_lines: list[str],
    aligned_words: list[dict[str, Any]],
    audio_duration: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    flattened_lyrics = [(line_index, token) for line_index, line in enumerate(lyrics_lines) for token in _split_words(line)]
    if not flattened_lyrics:
        return [], []

    if audio_duration <= 0:
        audio_duration = max(len(flattened_lyrics) * 0.25, 1.0)
    default_duration = max(audio_duration / max(len(flattened_lyrics), 1), 0.12)

    words: list[dict[str, Any]] = []
    line_to_word_ids: dict[int, list[str]] = {index: [] for index in range(len(lyrics_lines))}
    fallback_cursor = 0.0
    aligned_cursor = 0

    for index, (line_index, token) in enumerate(flattened_lyrics):
        if aligned_cursor < len(aligned_words):
            aligned = aligned_words[aligned_cursor]
            start = float(aligned["start"])
            end = float(aligned["end"])
            fallback_cursor = end
            aligned_cursor += 1
        else:
            start = max(fallback_cursor, 0.0)
            end = start + default_duration
            fallback_cursor = end

        if end <= start:
            end = start + max(default_duration, 0.12)

        word_id = f"word_{index:04d}"
        line_to_word_ids.setdefault(line_index, []).append(word_id)
        words.append(
            {
                "id": word_id,
                "index": index,
                "line_index": line_index,
                "text": token,
                "start": round(start, 3),
                "end": round(end, 3),
            }
        )

    word_lookup = {word["id"]: word for word in words}
    lines_payload: list[dict[str, Any]] = []
    for line_index, line_text in enumerate(lyrics_lines):
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
                "text": line_text,
                "start": round(start, 3),
                "end": round(end, 3),
                "word_ids": word_ids,
            }
        )

    return words, lines_payload


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def _copy_if_exists(source: Path, destination: Path) -> None:
    if not str(source).strip():
        return
    if not source.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() == destination.resolve():
        return
    shutil.copyfile(source, destination)


def _build_remaining_word_stream(
    remaining_words_templates: list[dict[str, Any]],
    aligned_words: list[dict[str, Any]],
    audio_duration: float,
    start_time_offset: float,
) -> list[dict[str, Any]]:
    if not remaining_words_templates:
        return []

    if audio_duration <= 0:
        audio_duration = max(len(remaining_words_templates) * 0.25, 1.0)
    default_duration = max(audio_duration / max(len(remaining_words_templates), 1), 0.12)

    words: list[dict[str, Any]] = []
    fallback_cursor = 0.0
    aligned_cursor = 0

    for index, word_template in enumerate(remaining_words_templates):
        if aligned_cursor < len(aligned_words):
            aligned = aligned_words[aligned_cursor]
            start = float(aligned["start"])
            end = float(aligned["end"])
            fallback_cursor = end
            aligned_cursor += 1
        else:
            start = max(fallback_cursor, 0.0)
            end = start + default_duration
            fallback_cursor = end

        if end <= start:
            end = start + max(default_duration, 0.12)

        shifted_start = round(start + start_time_offset, 3)
        shifted_end = round(end + start_time_offset, 3)

        w = dict(word_template)
        w["start"] = shifted_start
        w["end"] = shifted_end
        words.append(w)

    return words


def export_editor_project(
    config: str | Path | Mapping[str, Any],
    *,
    project_name: str,
    source_name: str,
    original_audio_name: str,
    lyrics_text: str,
    audio_artifacts: Mapping[str, str | Path],
    aligned_transcript: Mapping[str, Any],
    lyrics_source_url: str = "",
    project_key: str | None = None,
    existing_overrides: dict[str, Any] | None = None,
    start_time_offset: float = 0.0,
    placed_word_count: int = 0,
) -> dict[str, Any]:
    """Write an editor project with all words already committed."""
    config_path = Path(config) if not isinstance(config, Mapping) else None
    config_data = load_config(config) if config_path else dict(config)
    paths = _paths_section(config_data)
    subtitle_settings = _subtitle_section(config_data)

    if config_path:
        root_dir = config_path.parent
    elif audio_artifacts.get("audio_wav"):
        root_dir = Path(audio_artifacts["audio_wav"]).resolve().parent.parent
    else:
        root_dir = Path.cwd()

    temp_dir = root_dir / "data"
    input_dir = root_dir / "data" / "input"
    temp_dir.mkdir(parents=True, exist_ok=True)
    input_dir.mkdir(parents=True, exist_ok=True)

    project_name = str(project_name).strip() or "Project"
    project_key = project_key or _project_storage_key(project_name, "project")
    project_dir = temp_dir / "projects" / project_key
    project_dir.mkdir(parents=True, exist_ok=True)

    manifest_name = str(subtitle_settings.get("manifest_name", "subtitles_manifest.json"))
    overrides_name = str(subtitle_settings.get("timing_overrides_name", "timing_overrides.json"))
    editor_manifest_path = project_dir / "subtitles" / "timing_editor_manifest.json"

    lyrics_lines = _clean_lines(lyrics_text)
    audio_duration = 0.0
    audio_wav = Path(str(audio_artifacts.get("audio_wav", "")))
    if audio_wav.exists():
        audio_duration = _read_wav_duration(audio_wav)

    aligned_words = _flatten_aligned_words(aligned_transcript)

    is_incremental = start_time_offset > 0.0 and placed_word_count > 0
    if is_incremental and not editor_manifest_path.exists():
        LOGGER.warning(
            "Incremental AI pass requested (offset=%.3f, placed=%d) but editor manifest missing at %s; falling back to full alignment",
            start_time_offset,
            placed_word_count,
            editor_manifest_path,
        )
        is_incremental = False

    if is_incremental:
        import json
        try:
            with editor_manifest_path.open("r", encoding="utf-8") as handle:
                existing_manifest = json.load(handle)
        except Exception:
            existing_manifest = {}

        original_words = existing_manifest.get("words", [])
        original_words = sorted(original_words, key=lambda w: w.get("index", 0))

        words = []
        for i in range(min(placed_word_count, len(original_words))):
            w = dict(original_words[i])
            w_id = w["id"]
            if existing_overrides and w_id in existing_overrides.get("words", {}):
                # Sanitize negative start/end times from corrupted persisted data.
                # Negative times are nonsensical for lyrics timing and would cause
                # all remaining words (shifted by start_time_offset) to also be
                # negative, making the whole project invisible in the wave editor.
                start_value = float(existing_overrides["words"][w_id]["start"])
                end_value = float(existing_overrides["words"][w_id]["end"])
                w["start"] = max(0.0, start_value)
                w["end"] = max(w["start"] + 0.05, end_value)
            words.append(w)

        remaining_templates = original_words[placed_word_count:]
        remaining_words = _build_remaining_word_stream(
            remaining_templates,
            aligned_words,
            audio_duration - start_time_offset,
            start_time_offset,
        )
        words.extend(remaining_words)

        word_lookup = {w["id"]: w for w in words}
        line_entries = []
        for line in existing_manifest.get("lines", []):
            line_copy = dict(line)
            word_ids = line_copy.get("word_ids", [])
            if word_ids:
                line_copy["start"] = float(word_lookup[word_ids[0]]["start"])
                line_copy["end"] = float(word_lookup[word_ids[-1]]["end"])
            line_entries.append(line_copy)

    else:
        words, line_entries = _build_word_stream(lyrics_lines, aligned_words, audio_duration)

        if existing_overrides:
            manual_words = existing_overrides.get("words", {})
            if manual_words:
                for w in words:
                    w_id = w.get("id")
                    if w_id in manual_words:
                        w["start"] = float(manual_words[w_id].get("start", w["start"]))
                        w["end"] = float(manual_words[w_id].get("end", w["end"]))

                word_lookup = {w["id"]: w for w in words}
                for line in line_entries:
                    word_ids = line.get("word_ids", [])
                    if word_ids:
                        line["start"] = float(word_lookup[word_ids[0]]["start"])
                        line["end"] = float(word_lookup[word_ids[-1]]["end"])

    intro = {
        "intro_title": project_name,
        "intro_subtitle": "",
        "intro_duration_seconds": 2.5,
        "intro_font_multiplier": 2.0,
    }

    manifest_payload = {
        "version": 1,
        "source_audio": str(audio_artifacts.get("audio_wav", "")),
        "lines": line_entries,
        "words": words,
        "intro": intro,
    }

    if existing_overrides:
        overrides_payload = dict(existing_overrides)
        overrides_payload["exported_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        # The AI pass always commits all words: the user's manually-placed
        # words retain their original times (preserved above at lines
        # 338-344), and the AI-aligned remainder becomes committed too. This
        # matches the user's expectation that "AI pass = place everything"
        # regardless of whether any words were pre-committed.
        overrides_payload["placed_word_count"] = len(words)
        overrides_payload["lyrics_text"] = "\n".join(lyrics_lines)
        overrides_payload["words"] = dict(overrides_payload.get("words", {}))
        for word in words:
            overrides_payload["words"][word["id"]] = {
                "start": float(word["start"]),
                "end": float(word["end"]),
            }
    else:
        overrides_payload = {
            "version": 1,
            "exported_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "global_offset": 0.0,
            "placed_word_count": len(words),
            "lyrics_text": "\n".join(lyrics_lines),
            "lines": {},
            "words": {
                word["id"]: {
                    "start": float(word["start"]),
                    "end": float(word["end"]),
                }
                for word in words
            },
        }

    state_payload = {
        "version": 1,
        "mode": "autosync",
        "status": "ready",
        "project_name": project_name,
        "source_name": source_name,
        "original_audio_name": original_audio_name,
        "audio_source": str(audio_artifacts.get("audio_source", "")),
        "lyrics_text": "\n".join(lyrics_lines),
        "lyrics_source_url": str(lyrics_source_url).strip(),
        "line_count": len(lyrics_lines),
        "word_count": len(words),
        "duration_seconds": audio_duration,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "artifacts": {key: str(Path(str(value))) for key, value in audio_artifacts.items() if str(value).strip()},
    }

    audio_path = project_dir / "audio" / "audio.wav"
    vocals_path = project_dir / "audio" / "vocals.wav"
    no_vocals_path = project_dir / "audio" / "no_vocals.wav"
    _copy_if_exists(Path(str(audio_artifacts.get("audio_wav", "")).strip()), audio_path)
    _copy_if_exists(Path(str(audio_artifacts.get("vocals_wav", "")).strip()), vocals_path)
    _copy_if_exists(Path(str(audio_artifacts.get("no_vocals_wav", "")).strip()), no_vocals_path)

    state_payload["artifacts"] = {
        "audio_wav": str(audio_path),
        "vocals_wav": str(vocals_path if vocals_path.exists() else audio_path),
        "no_vocals_wav": str(no_vocals_path if no_vocals_path.exists() else vocals_path if vocals_path.exists() else audio_path),
    }

    editor_manifest_path = project_dir / "subtitles" / "timing_editor_manifest.json"
    subtitles_manifest_path = project_dir / "subtitles" / manifest_name
    overrides_path = project_dir / "state" / overrides_name
    state_path = project_dir / "state" / "state.json"
    lyrics_path = project_dir / "subtitles" / "lyrics.txt"

    _write_json_atomic(editor_manifest_path, manifest_payload)
    if subtitles_manifest_path != editor_manifest_path:
        _write_json_atomic(subtitles_manifest_path, manifest_payload)
    _write_json_atomic(overrides_path, overrides_payload)
    _write_json_atomic(state_path, state_payload)
    lyrics_path.write_text("\n".join(lyrics_lines), encoding="utf-8")

    current_marker = root_dir / "data" / ".current_project"
    current_marker.parent.mkdir(parents=True, exist_ok=True)
    current_marker.write_text(project_key, encoding="utf-8")

    return {
        "project_id": project_key,
        "project_name": project_name,
        "project_dir": str(project_dir),
        "state_path": str(state_path),
        "manifest_path": str(editor_manifest_path),
        "subtitles_manifest_path": str(subtitles_manifest_path),
        "overrides_path": str(overrides_path),
        "lyrics_path": str(lyrics_path),
        "current_project_marker": str(current_marker),
        "state": state_payload,
        "manifest": manifest_payload,
        "overrides": overrides_payload,
    }
