"""Transcribe audio locally with Whisper.

This stage loads a local Whisper model, produces a timestamped segment list,
and writes both JSON and plain-text transcript outputs into temp/.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path
from typing import Any, Mapping

import yaml

LOGGER = logging.getLogger(__name__)


def load_config(config: str | Path | Mapping[str, Any]) -> dict[str, Any]:
    """Load pipeline configuration from a YAML file or mapping."""
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


def _transcriber_section(config: Mapping[str, Any]) -> Mapping[str, Any]:
    settings = config.get("transcriber")
    if not isinstance(settings, Mapping):
        raise KeyError("Missing 'transcriber' section in config")
    return settings


def _load_whisper_module():
    try:
        import whisper  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise ModuleNotFoundError(
            "The 'openai-whisper' package is required for transcription."
        ) from exc
    return whisper


def build_transcriber_settings(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return the normalized transcriber settings from config."""
    settings = _transcriber_section(config)
    return {
        "model_name": str(settings["model_name"]),
        "device": str(settings.get("device", "cpu")),
        "language": str(settings.get("language", "he")),
        "task": str(settings.get("task", "transcribe")),
        "fp16": bool(settings.get("fp16", False)),
        "output_json_name": str(settings.get("output_json_name", "transcript.json")),
        "output_text_name": str(settings.get("output_text_name", "transcript.txt")),
    }


def _write_transcript_files(
    transcript: Mapping[str, Any],
    json_path: Path,
    text_path: Path,
) -> None:
    json_path.write_text(
        json.dumps(transcript, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    text_path.write_text(str(transcript.get("text", "")), encoding="utf-8")


def transcribe_audio(
    input_file: str | Path,
    config: str | Path | Mapping[str, Any],
) -> dict[str, Any]:
    """Transcribe audio with Whisper and save transcript artifacts under temp/."""
    config_data = load_config(config)
    input_path = Path(input_file)

    if not input_path.exists():
        raise FileNotFoundError(f"Input file does not exist: {input_path}")

    paths = _paths_section(config_data)
    settings = build_transcriber_settings(config_data)
    temp_dir = Path(paths["temp_dir"])
    temp_dir.mkdir(parents=True, exist_ok=True)

    json_path = temp_dir / settings["output_json_name"]
    text_path = temp_dir / settings["output_text_name"]

    whisper = _load_whisper_module()
    start = time.perf_counter()
    LOGGER.info("transcriber:start input=%s model=%s", input_path, settings["model_name"])

    model = whisper.load_model(
        settings["model_name"],
        device=settings["device"],
    )
    result = model.transcribe(
        str(input_path),
        language=settings["language"],
        task=settings["task"],
        fp16=settings["fp16"],
        verbose=False,
    )

    transcript = {
        "source_file": str(input_path),
        "model_name": settings["model_name"],
        "language": result.get("language", settings["language"]),
        "text": result.get("text", ""),
        "segments": result.get("segments", []),
    }
    _write_transcript_files(transcript, json_path, text_path)

    duration = time.perf_counter() - start
    LOGGER.info(
        "transcriber:end json=%s text=%s duration_seconds=%.2f",
        json_path,
        text_path,
        duration,
    )
    transcript["json_path"] = str(json_path)
    transcript["text_path"] = str(text_path)
    return transcript


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Transcribe audio with a local Whisper model.",
    )
    parser.add_argument("input_file", help="Input audio file, e.g. temp/no_vocals.wav")
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml (default: config.yaml)",
    )
    return parser


def main() -> int:
    parser = _build_arg_parser()
    args = parser.parse_args()
    transcribe_audio(args.input_file, args.config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
