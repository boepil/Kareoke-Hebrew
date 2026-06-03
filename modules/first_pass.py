"""First-pass autosync orchestration.

This is the phase-2 path: separate vocals, transcribe with Gemma/Whisper,
optionally correct against fetched lyrics through a local LM Studio endpoint,
align with WhisperX, and materialize an editor-ready project with all words
already committed.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import re
import time
import shutil
import wave
from pathlib import Path
from typing import Any, Mapping
import logging

LOGGER = logging.getLogger(__name__)

from modules.audio_extractor import extract_and_normalize_audio
from modules.editor_project import export_editor_project
from modules.lyrics_corrector import correct_transcript_with_lm_studio
from modules.separator import separate_vocals
from modules.transcriber import transcribe_audio
from modules.aligner import align_transcript


ProgressCallback = Any


def _emit_progress(callback: ProgressCallback | None, stage_key: str, status: str, detail: str = "") -> None:
    if callback is None:
        return
    try:
        callback(stage_key=stage_key, status=status, detail=detail)
    except Exception:
        pass


def _has_existing_stems(vocals_path: Path, no_vocals_path: Path) -> bool:
    return (
        vocals_path.exists()
        and no_vocals_path.exists()
        and vocals_path.stat().st_size > 0
        and no_vocals_path.stat().st_size > 0
    )


def _clean_lyrics_lines(raw_text: str) -> list[str]:
    lines = [re.sub(r"\s+", " ", line).strip() for line in str(raw_text or "").splitlines()]
    return [line for line in lines if line]


def _read_wav_duration_seconds(audio_path: Path) -> float:
    if not audio_path.exists():
        return 0.0
    with wave.open(str(audio_path), "rb") as wav_file:
        frame_rate = wav_file.getframerate()
        if frame_rate <= 0:
            return 0.0
        total_frames = wav_file.getnframes()
    return max(float(total_frames) / float(frame_rate), 0.0)


def _build_lyrics_seed_transcript(vocals_path: Path, lyrics_text: str) -> dict[str, Any]:
    lines = _clean_lyrics_lines(lyrics_text)
    duration_seconds = _read_wav_duration_seconds(vocals_path)
    if not lines:
        return {
            "source_file": str(vocals_path),
            "provider": "lyrics_seed",
            "model_name": "lyrics_seed",
            "language": "he",
            "text": "",
            "segments": [],
        }

    if duration_seconds <= 0:
        duration_seconds = max(float(len(lines)) * 2.0, 1.0)
    slot = max(duration_seconds / max(len(lines), 1), 0.25)

    segments: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        start = index * slot
        end = min((index + 1) * slot, duration_seconds)
        if end <= start:
            end = start + 0.25
        segments.append(
            {
                "id": index,
                "start": round(start, 3),
                "end": round(end, 3),
                "text": line,
            }
        )

    return {
        "source_file": str(vocals_path),
        "provider": "lyrics_seed",
        "model_name": "lyrics_seed",
        "language": "he",
        "text": "\n".join(lines),
        "segments": segments,
    }


def _project_storage_key(name: str) -> str:
    cleaned = Path(str(name or "").strip()).name
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        cleaned = "project"
    return cleaned.replace(".", "_")


def _copy_if_exists(source: Path, destination: Path) -> None:
    if not source.exists() or source.stat().st_size <= 0:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() == destination.resolve():
        return
    shutil.copy2(source, destination)


def slice_wav(input_path: Path, output_path: Path, start_time: float) -> None:
    """Slice a WAV file from start_time to the end and save it to output_path."""
    with wave.open(str(input_path), "rb") as infile:
        params = infile.getparams()
        frame_rate = params.framerate
        start_frame = int(start_time * frame_rate)
        total_frames = infile.getnframes()
        if start_frame >= total_frames:
            start_frame = max(0, total_frames - 1)
        infile.setpos(start_frame)
        frames_to_read = total_frames - start_frame
        if frames_to_read <= 0:
            frames = b""
        else:
            frames = infile.readframes(frames_to_read)

    with wave.open(str(output_path), "wb") as outfile:
        outfile.setparams(params)
        outfile.writeframes(frames)


def run_first_pass_autosync(
    input_file: str | Path,
    config: str | Path | Mapping[str, Any],
    *,
    project_name: str,
    source_name: str,
    original_audio_name: str,
    lyrics_text: str = "",
    lyrics_source_url: str = "",
    project_key: str | None = None,
    progress_callback: ProgressCallback | None = None,
    existing_overrides: dict[str, Any] | None = None,
    start_time_offset: float = 0.0,
    placed_word_count: int = 0,
) -> dict[str, Any]:
    """Run the phase-2 sync path and write an editor-ready project."""
    config_data = config if isinstance(config, Mapping) else None
    input_path = Path(input_file)

    if config_data is None:
        from modules.editor_project import load_config as _load_config

        config_data = _load_config(config)
    paths = config_data.get("paths")
    temp_dir = Path(paths["temp_dir"]) if isinstance(paths, Mapping) else Path("temp")
    resolved_project_key = str(project_key or "").strip() or _project_storage_key(project_name)
    project_temp_dir = temp_dir / "projects" / resolved_project_key
    project_temp_dir.mkdir(parents=True, exist_ok=True)

    project_vocals_path = project_temp_dir / "audio" / "vocals.wav"
    project_no_vocals_path = project_temp_dir / "audio" / "no_vocals.wav"
    project_audio_path = project_temp_dir / "audio" / "audio.wav"

    # Legacy fallback path from older global-temp flow.
    legacy_audio_path = temp_dir / "audio.wav"

    vocals_path = project_vocals_path
    no_vocals_path = project_no_vocals_path

    _emit_progress(progress_callback, "stem_separation", "running", "Preparing vocal stems")
    # Refresh stems only if they don't exist or are invalid.
    extracted_audio = extract_and_normalize_audio(input_path, legacy_audio_path, config)
    
    if _has_existing_stems(project_vocals_path, project_no_vocals_path):
        LOGGER.info("Using existing stems for project %s", resolved_project_key)
        produced_vocals = project_vocals_path
        produced_no_vocals = project_no_vocals_path
        extracted_audio = project_audio_path if project_audio_path.exists() else extracted_audio
    else:
        separation = separate_vocals(extracted_audio, config)
        produced_vocals = Path(str(separation.get("vocals", input_path.with_name("vocals.wav"))))
        produced_no_vocals = Path(str(separation.get("no_vocals", input_path.with_name("no_vocals.wav"))))
        _copy_if_exists(produced_vocals, project_vocals_path)
        _copy_if_exists(produced_no_vocals, project_no_vocals_path)
        _copy_if_exists(legacy_audio_path, project_audio_path)

    if _has_existing_stems(project_vocals_path, project_no_vocals_path):
        vocals_path = project_vocals_path
        no_vocals_path = project_no_vocals_path
    else:
        vocals_path = produced_vocals
        no_vocals_path = produced_no_vocals

    alignment_vocals_path = vocals_path
    alignment_lyrics_text = lyrics_text

    if start_time_offset > 0.0:
        sliced_vocals_path = project_temp_dir / "audio" / "vocals_remaining.wav"
        LOGGER.info("Slicing vocals audio from %f seconds for incremental AI Pass", start_time_offset)
        slice_wav(vocals_path, sliced_vocals_path, start_time_offset)
        alignment_vocals_path = sliced_vocals_path

        if placed_word_count > 0:
            manifest_path = project_temp_dir / "subtitles" / "timing_editor_manifest.json"
            if manifest_path.exists():
                try:
                    with manifest_path.open("r", encoding="utf-8") as handle:
                        manifest = json.load(handle)
                    manifest_words = sorted(manifest.get("words", []), key=lambda w: w.get("index", 0))
                    remaining_words = manifest_words[placed_word_count:]
                    alignment_lyrics_text = " ".join([w["text"] for w in remaining_words])
                except Exception as exc:
                    LOGGER.warning("Could not extract remaining lyrics text for alignment: %s", exc)

    _emit_progress(progress_callback, "stem_separation", "done", "Vocal stems ready")

    _emit_progress(progress_callback, "transcription", "running", "Transcribing vocals")
    transcript_fallback = False
    try:
        transcript = transcribe_audio(alignment_vocals_path, config)
        segments = transcript.get("segments", [])
        if not isinstance(segments, list) or not segments:
            raise RuntimeError("Transcription returned no segments")
        _emit_progress(progress_callback, "transcription", "done", "Transcription complete")
    except Exception as exc:
        transcript_fallback = True
        transcript = _build_lyrics_seed_transcript(alignment_vocals_path, alignment_lyrics_text)
        _emit_progress(
            progress_callback,
            "transcription",
            "skipped",
            f"Using lyrics-seeded timeline fallback ({type(exc).__name__})",
        )

    correction_status = "skipped"
    _emit_progress(progress_callback, "lm_studio_correction", "running", "Checking lyric correction")
    corrected_transcript = correct_transcript_with_lm_studio(transcript, alignment_lyrics_text, config)
    correction_status = str(corrected_transcript.get("correction_status", "skipped"))
    _emit_progress(
        progress_callback,
        "lm_studio_correction",
        "skipped" if correction_status == "skipped" else "done",
        "Correction skipped" if correction_status == "skipped" else "Correction applied",
    )

    _emit_progress(progress_callback, "whisperx_alignment", "running", "Aligning words")
    try:
        aligned = align_transcript(alignment_vocals_path, corrected_transcript, config)
        _emit_progress(progress_callback, "whisperx_alignment", "done", "Alignment complete")
    except Exception as exc:
        aligned = dict(corrected_transcript)
        aligned["json_path"] = ""
        _emit_progress(
            progress_callback,
            "whisperx_alignment",
            "skipped",
            f"Alignment unavailable, continuing with lyrics timeline ({type(exc).__name__})",
        )

    audio_artifacts = {
        "audio_source": input_path,
        "audio_wav": extracted_audio,
        "vocals_wav": vocals_path,
        "no_vocals_wav": no_vocals_path,
        "transcript_json": transcript.get("json_path", ""),
        "transcript_text": transcript.get("text_path", ""),
        "aligned_json": aligned.get("json_path", ""),
    }
    _emit_progress(progress_callback, "editor_project", "running", "Building editor project")
    project = export_editor_project(
        config,
        project_name=project_name,
        source_name=source_name,
        original_audio_name=original_audio_name,
        lyrics_text=lyrics_text or str(corrected_transcript.get("text", "")).strip(),
        audio_artifacts=audio_artifacts,
        aligned_transcript=aligned,
        lyrics_source_url=lyrics_source_url,
        project_key=project_key,
        existing_overrides=existing_overrides,
        start_time_offset=start_time_offset,
        placed_word_count=placed_word_count,
    )
    _emit_progress(progress_callback, "editor_project", "done", "Editor project ready")
    return {
        "extracted_audio": str(extracted_audio),
        "vocals_wav": str(vocals_path),
        "no_vocals_wav": str(no_vocals_path),
        "transcript": corrected_transcript,
        "aligned": aligned,
        "correction_status": correction_status,
        "transcript_fallback": transcript_fallback,
        "project": project,
    }
