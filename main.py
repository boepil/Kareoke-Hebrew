"""CLI entrypoint for the local Hebrew karaoke pipeline."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any, Mapping

import yaml

from modules.aligner import align_transcript
from modules.audio_extractor import extract_and_normalize_audio
from modules.editor_project import export_editor_project
from modules.lyrics_corrector import correct_transcript_with_lm_studio
from modules.lyrics_source import import_lyrics
from modules.renderer import render_video
from modules.separator import separate_vocals
from modules.subtitle_builder import build_subtitles
from modules.transcriber import transcribe_audio

STAGE_LYRICS = "lyrics_imported"
STAGE_AUDIO = "audio_extracted"
STAGE_SEPARATOR = "separated"
STAGE_TRANSCRIBER = "transcribed"
STAGE_ALIGNER = "aligned"
STAGE_SUBTITLES = "subtitles_built"
STAGE_RENDERER = "rendered"

PIPELINE_STAGES = (
    STAGE_AUDIO,
    STAGE_SEPARATOR,
    STAGE_TRANSCRIBER,
    STAGE_ALIGNER,
    STAGE_SUBTITLES,
    STAGE_RENDERER,
)

LOGGER = logging.getLogger(__name__)


def load_config(config_path: str | Path) -> dict[str, Any]:
    config_file = Path(config_path)
    with config_file.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Config file must contain a mapping: {config_file}")
    return loaded


def _paths_section(config: Mapping[str, Any]) -> Mapping[str, Any]:
    paths = config.get("paths")
    if not isinstance(paths, Mapping):
        raise KeyError("Missing 'paths' section in config")
    return paths


def _render_output_path(config: Mapping[str, Any]) -> Path:
    paths = _paths_section(config)
    renderer = config.get("renderer")
    if not isinstance(renderer, Mapping):
        raise KeyError("Missing 'renderer' section in config")
    return Path(paths["output_dir"]) / str(renderer.get("output_video_name", "karaoke.mp4"))


def _artifact_paths(config: Mapping[str, Any]) -> dict[str, str]:
    paths = _paths_section(config)
    renderer = config.get("renderer")
    if not isinstance(renderer, Mapping):
        raise KeyError("Missing 'renderer' section in config")
    lyrics = config.get("lyrics_source")
    if not isinstance(lyrics, Mapping):
        lyrics = {}

    temp_dir = Path(paths["temp_dir"])
    output_dir = Path(paths["output_dir"])
    return {
        "audio_wav": str(temp_dir / "audio.wav"),
        "vocals_wav": str(temp_dir / "vocals.wav"),
        "no_vocals_wav": str(temp_dir / "no_vocals.wav"),
        "lyrics_text": str(temp_dir / str(lyrics.get("output_text_name", "lyrics.txt"))),
        "lyrics_json": str(temp_dir / str(lyrics.get("output_json_name", "lyrics.json"))),
        "transcript_json": str(temp_dir / "transcript.json"),
        "transcript_text": str(temp_dir / "transcript.txt"),
        "aligned_json": str(temp_dir / "aligned.json"),
        "subtitles_ass": str(temp_dir / "subtitles.ass"),
        "output_video": str(output_dir / str(renderer.get("output_video_name", "karaoke.mp4"))),
    }


def _resolve_audio_artifact(
    config: Mapping[str, Any],
    state: Mapping[str, Any],
    section_name: str,
    default_artifact: str,
) -> Path:
    section = config.get(section_name)
    if not isinstance(section, Mapping):
        raise KeyError(f"Missing '{section_name}' section in config")

    artifact_name = str(section.get("audio_artifact", default_artifact))
    artifacts = state.get("artifacts", {})
    if not isinstance(artifacts, Mapping):
        raise ValueError("State artifacts must be a mapping")

    artifact_path = artifacts.get(artifact_name)
    if not artifact_path:
        raise KeyError(
            f"Unknown audio artifact '{artifact_name}' configured in '{section_name}.audio_artifact'"
        )

    resolved = Path(str(artifact_path))
    if not resolved.exists():
        raise FileNotFoundError(
            f"Configured audio artifact for '{section_name}' does not exist: {resolved}"
        )
    return resolved


def _initial_state(input_file: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "input_file": str(input_file),
        "status": "pending",
        "current_stage": None,
        "completed_stages": [],
        "last_error": None,
        "artifacts": _artifact_paths(config),
    }


def _load_state(state_path: Path) -> dict[str, Any]:
    if not state_path.exists():
        return {}
    with state_path.open("r", encoding="utf-8") as handle:
        loaded = json.load(handle)
    if not isinstance(loaded, dict):
        raise ValueError(f"State file must contain an object: {state_path}")
    return loaded


def _save_state(state_path: Path, state: Mapping[str, Any]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _configure_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
        handler.close()

    root_logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)


def _shutdown_logging() -> None:
    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        handler.flush()
        handler.close()
        root_logger.removeHandler(handler)


def _stage_completed(state: Mapping[str, Any], stage_name: str) -> bool:
    completed = state.get("completed_stages", [])
    return isinstance(completed, list) and stage_name in completed


def _mark_stage_complete(
    state: dict[str, Any],
    stage_name: str,
    artifact_updates: Mapping[str, str] | None = None,
) -> None:
    completed = state.setdefault("completed_stages", [])
    if stage_name not in completed:
        completed.append(stage_name)
    if artifact_updates:
        artifacts = state.setdefault("artifacts", {})
        artifacts.update(dict(artifact_updates))
    state["current_stage"] = None
    state["status"] = "running"
    state["last_error"] = None


def _ensure_artifacts_exist(state: Mapping[str, Any], keys: list[str]) -> bool:
    artifacts = state.get("artifacts", {})
    if not isinstance(artifacts, Mapping):
        return False
    return all(Path(str(artifacts.get(key, ""))).exists() for key in keys)


def process(input_file: str | Path, config_path: str | Path = "config.yaml") -> Path:
    """Run the full karaoke pipeline and return the rendered MP4 path."""
    config = load_config(config_path)
    paths = _paths_section(config)

    input_path = Path(input_file).resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"Input file does not exist: {input_path}")

    temp_dir = Path(paths["temp_dir"])
    output_dir = Path(paths["output_dir"])
    logs_dir = Path(paths["logs_dir"])
    temp_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    _configure_logging(logs_dir / "pipeline.log")

    state_path = temp_dir / "state.json"
    state = _load_state(state_path)
    if state.get("input_file") != str(input_path):
        state = _initial_state(input_path, config)
        _save_state(state_path, state)
    elif "artifacts" not in state:
        state["artifacts"] = _artifact_paths(config)

    LOGGER.info("pipeline:start input=%s", input_path)

    def persist() -> None:
        _save_state(state_path, state)

    try:
        autosync_settings = config.get("autosync")
        if not isinstance(autosync_settings, Mapping):
            autosync_settings = {}
        transcriber_settings = config.get("transcriber")
        transcriber_provider = ""
        if isinstance(transcriber_settings, Mapping):
            transcriber_provider = str(transcriber_settings.get("provider", "")).strip().lower()
        autosync_enabled = bool(autosync_settings.get("enabled", False)) or transcriber_provider == "gemma"

        if not _stage_completed(state, STAGE_AUDIO) or not _ensure_artifacts_exist(state, ["audio_wav"]):
            state["current_stage"] = STAGE_AUDIO
            persist()
            audio_path = extract_and_normalize_audio(input_path, state["artifacts"]["audio_wav"], config)
            _mark_stage_complete(state, STAGE_AUDIO, {"audio_wav": str(audio_path)})
            persist()

        lyrics_settings = config.get("lyrics_source")
        if isinstance(lyrics_settings, Mapping):
            enabled = bool(lyrics_settings.get("enabled", False))
            source_url = str(lyrics_settings.get("source_url", "")).strip()
        else:
            enabled = False
            source_url = ""

        if enabled and source_url and (
            not _stage_completed(state, STAGE_LYRICS)
            or not _ensure_artifacts_exist(state, ["lyrics_text", "lyrics_json"])
        ):
            state["current_stage"] = STAGE_LYRICS
            persist()
            lyrics = import_lyrics(source_url, config)
            _mark_stage_complete(
                state,
                STAGE_LYRICS,
                {
                    "lyrics_text": state["artifacts"]["lyrics_text"],
                    "lyrics_json": state["artifacts"]["lyrics_json"],
                },
            )
            persist()

        if not _stage_completed(state, STAGE_SEPARATOR) or not _ensure_artifacts_exist(state, ["vocals_wav", "no_vocals_wav"]):
            state["current_stage"] = STAGE_SEPARATOR
            persist()
            separation = separate_vocals(state["artifacts"]["audio_wav"], config)
            _mark_stage_complete(
                state,
                STAGE_SEPARATOR,
                {
                    "vocals_wav": str(separation["vocals"]),
                    "no_vocals_wav": str(separation["no_vocals"]),
                },
            )
            persist()

        if not _stage_completed(state, STAGE_TRANSCRIBER) or not _ensure_artifacts_exist(state, ["transcript_json", "transcript_text"]):
            state["current_stage"] = STAGE_TRANSCRIBER
            persist()
            transcription_audio = _resolve_audio_artifact(config, state, "transcriber", "vocals_wav")
            transcript = transcribe_audio(transcription_audio, config)
            if autosync_enabled:
                lyrics_text = ""
                if _stage_completed(state, STAGE_AUDIO):
                    lyrics_artifacts = state.get("artifacts", {})
                    lyrics_path_raw = lyrics_artifacts.get("lyrics_text") if isinstance(lyrics_artifacts, Mapping) else ""
                    if lyrics_path_raw:
                        lyrics_path = Path(str(lyrics_path_raw))
                        if lyrics_path.exists():
                            try:
                                lyrics_text = lyrics_path.read_text(encoding="utf-8").strip()
                            except Exception:
                                lyrics_text = ""
                if lyrics_text:
                    transcript = correct_transcript_with_lm_studio(transcript, lyrics_text, config)
            _mark_stage_complete(
                state,
                STAGE_TRANSCRIBER,
                {
                    "transcript_json": str(transcript["json_path"]),
                    "transcript_text": str(transcript["text_path"]),
                },
            )
            persist()

        aligned_payload: dict[str, Any]
        if not _stage_completed(state, STAGE_ALIGNER) or not _ensure_artifacts_exist(state, ["aligned_json"]):
            state["current_stage"] = STAGE_ALIGNER
            persist()
            aligner_audio = _resolve_audio_artifact(config, state, "aligner", "vocals_wav")
            aligned_payload = align_transcript(
                aligner_audio,
                state["artifacts"]["transcript_json"],
                config,
            )
            _mark_stage_complete(
                state,
                STAGE_ALIGNER,
                {"aligned_json": str(aligned_payload["json_path"])},
            )
            persist()
        else:
            aligned_path = Path(str(state["artifacts"]["aligned_json"]))
            aligned_payload = _load_state(aligned_path)

        if autosync_enabled:
            lyrics_text = ""
            lyrics_artifacts = state.get("artifacts", {})
            if isinstance(lyrics_artifacts, Mapping):
                lyrics_path_raw = lyrics_artifacts.get("lyrics_text")
                if lyrics_path_raw:
                    lyrics_path = Path(str(lyrics_path_raw))
                    if lyrics_path.exists():
                        try:
                            lyrics_text = lyrics_path.read_text(encoding="utf-8").strip()
                        except Exception:
                            lyrics_text = ""
            if not lyrics_text and state["artifacts"].get("transcript_text"):
                transcript_text_path = Path(str(state["artifacts"]["transcript_text"]))
                if transcript_text_path.exists():
                    try:
                        lyrics_text = transcript_text_path.read_text(encoding="utf-8").strip()
                    except Exception:
                        lyrics_text = ""
            editor_project = export_editor_project(
                config,
                project_name=input_path.stem,
                source_name=input_path.name,
                original_audio_name=input_path.name,
                lyrics_text=lyrics_text,
                audio_artifacts={
                    "audio_source": input_path,
                    "audio_wav": state["artifacts"].get("audio_wav", ""),
                    "vocals_wav": state["artifacts"].get("vocals_wav", ""),
                    "no_vocals_wav": state["artifacts"].get("no_vocals_wav", ""),
                    "transcript_json": state["artifacts"].get("transcript_json", ""),
                    "transcript_text": state["artifacts"].get("transcript_text", ""),
                    "aligned_json": state["artifacts"].get("aligned_json", ""),
                },
                aligned_transcript=aligned_payload,
                lyrics_source_url=str(state.get("lyrics_source_url", "")).strip(),
            )
            state["project_name"] = editor_project["project_name"]
            state["editor_project_id"] = editor_project["project_id"]
            persist()

        if not _stage_completed(state, STAGE_SUBTITLES) or not _ensure_artifacts_exist(state, ["subtitles_ass"]):
            state["current_stage"] = STAGE_SUBTITLES
            persist()
            subtitles = build_subtitles(state["artifacts"]["aligned_json"], config)
            _mark_stage_complete(state, STAGE_SUBTITLES, {"subtitles_ass": str(subtitles)})
            persist()

        if not _stage_completed(state, STAGE_RENDERER) or not _ensure_artifacts_exist(state, ["output_video"]):
            state["current_stage"] = STAGE_RENDERER
            persist()
            render_audio = _resolve_audio_artifact(config, state, "renderer", "no_vocals_wav")
            output_video = render_video(
                render_audio,
                state["artifacts"]["subtitles_ass"],
                config,
            )
            _mark_stage_complete(state, STAGE_RENDERER, {"output_video": str(output_video)})
            persist()

        state["status"] = "completed"
        state["current_stage"] = None
        state["last_error"] = None
        persist()
        LOGGER.info("pipeline:end output=%s", state["artifacts"]["output_video"])
        return Path(state["artifacts"]["output_video"])
    except Exception as exc:
        state["status"] = "failed"
        state["last_error"] = f"{type(exc).__name__}: {exc}"
        persist()
        LOGGER.exception("pipeline:failed stage=%s", state.get("current_stage"))
        raise
    finally:
        _shutdown_logging()


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local Hebrew karaoke pipeline.")
    parser.add_argument("input_file", help="Input media file, e.g. input/test.mp3")
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml (default: config.yaml)",
    )
    return parser


def main() -> int:
    parser = _build_arg_parser()
    args = parser.parse_args()
    process(args.input_file, args.config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
