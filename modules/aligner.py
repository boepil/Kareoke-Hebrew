"""Align Whisper segments to word-level timestamps with WhisperX.

This stage enriches the segment-level transcript JSON with word timings and
writes the result to temp/aligned.json for karaoke subtitle generation.
"""

from __future__ import annotations

import os
import argparse
import json
import logging
import time
from pathlib import Path
from typing import Any, Mapping

import yaml

LOGGER = logging.getLogger(__name__)

# WhisperX pulls in transformers, which can otherwise probe TensorFlow on import.
# Disabling TF keeps the local alignment path on the PyTorch/NumPy stack.
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("TRANSFORMERS_NO_TF", "1")
os.environ.setdefault("USE_FLAX", "0")


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


def _aligner_section(config: Mapping[str, Any]) -> Mapping[str, Any]:
    settings = config.get("aligner")
    if not isinstance(settings, Mapping):
        raise KeyError("Missing 'aligner' section in config")
    return settings


def _load_whisperx_module():
    try:
        import whisperx  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise ModuleNotFoundError(
            "The 'whisperx' package is required for word-level alignment."
        ) from exc
    return whisperx


def build_aligner_settings(config: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize aligner settings from config."""
    settings = _aligner_section(config)
    return {
        "device": str(settings.get("device", "cpu")),
        "compute_type": str(settings.get("compute_type", "int8")),
        "output_json_name": str(settings.get("output_json_name", "aligned.json")),
    }


def _read_json_source(source: str | Path | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(source, Mapping):
        return dict(source)

    source_path = Path(source)
    with source_path.open("r", encoding="utf-8") as handle:
        loaded = json.load(handle)
    if not isinstance(loaded, dict):
        raise ValueError(f"Transcript file must contain an object: {source_path}")
    return loaded


def align_transcript(
    audio_file: str | Path,
    transcript_source: str | Path | Mapping[str, Any],
    config: str | Path | Mapping[str, Any],
) -> dict[str, Any]:
    """Align segment timestamps to word-level timings using WhisperX."""
    config_data = load_config(config)
    audio_path = Path(audio_file)
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file does not exist: {audio_path}")

    transcript = _read_json_source(transcript_source)
    segments = transcript.get("segments", [])
    if not isinstance(segments, list):
        raise ValueError("Transcript segments must be a list")

    paths = _paths_section(config_data)
    settings = build_aligner_settings(config_data)
    temp_dir = Path(paths["temp_dir"])
    temp_dir.mkdir(parents=True, exist_ok=True)
    output_path = temp_dir / settings["output_json_name"]

    whisperx = _load_whisperx_module()
    language_code = str(transcript.get("language", "he"))

    start = time.perf_counter()
    LOGGER.info(
        "aligner:start audio=%s transcript=%s language=%s",
        audio_path,
        transcript_source,
        language_code,
    )

    audio = whisperx.load_audio(str(audio_path))
    model_a, metadata = whisperx.load_align_model(
        language_code=language_code,
        device=settings["device"],
    )
    aligned_result = whisperx.align(
        segments,
        model_a,
        metadata,
        audio,
        device=settings["device"],
        return_char_alignments=False,
    )

    if not isinstance(aligned_result, dict):
        raise ValueError("WhisperX alignment result must be a mapping")

    aligned_output = dict(aligned_result)
    aligned_output["source_file"] = str(audio_path)
    aligned_output["transcript_source"] = str(transcript_source)
    aligned_output["language"] = aligned_output.get("language", language_code)

    output_path.write_text(
        json.dumps(aligned_output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    duration = time.perf_counter() - start
    LOGGER.info("aligner:end output=%s duration_seconds=%.2f", output_path, duration)
    aligned_output["json_path"] = str(output_path)
    return aligned_output


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Align Whisper segments to word-level timestamps with WhisperX.",
    )
    parser.add_argument("audio_file", help="Audio file, e.g. temp/no_vocals.wav")
    parser.add_argument(
        "transcript_source",
        help="Transcript JSON file, e.g. temp/transcript.json",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml (default: config.yaml)",
    )
    return parser


def main() -> int:
    parser = _build_arg_parser()
    args = parser.parse_args()
    align_transcript(args.audio_file, args.transcript_source, args.config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
