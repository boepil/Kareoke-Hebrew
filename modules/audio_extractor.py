"""Extract and normalize audio with FFmpeg.

This stage turns an input media file into a 44.1 kHz WAV file suitable for
downstream separation and transcription. All tunables are sourced from
`config.yaml`.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
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


def _audio_settings(config: Mapping[str, Any]) -> Mapping[str, Any]:
    settings = config.get("audio_extractor")
    if not isinstance(settings, Mapping):
        raise KeyError("Missing 'audio_extractor' section in config")
    return settings


def build_ffmpeg_command(
    input_file: str | Path,
    output_file: str | Path,
    config: Mapping[str, Any],
) -> list[str]:
    """Build the FFmpeg command used for extraction and normalization."""
    settings = _audio_settings(config)

    ffmpeg_path = str(settings["ffmpeg_path"])
    sample_rate_hz = str(settings["sample_rate_hz"])
    channels = str(settings["channels"])
    audio_codec = str(settings["audio_codec"])
    normalization_filter = str(settings.get("normalization_filter", "")).strip()

    command = [
        ffmpeg_path,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(input_file),
        "-vn",
        "-map",
        "0:a:0",
        "-ac",
        channels,
        "-ar",
        sample_rate_hz,
        "-c:a",
        audio_codec,
        str(output_file),
    ]

    if normalization_filter:
        command[-1:-1] = ["-af", normalization_filter]

    return command


def extract_and_normalize_audio(
    input_file: str | Path,
    output_file: str | Path,
    config: str | Path | Mapping[str, Any],
) -> Path:
    """Extract the first audio track and normalize it to a WAV file."""
    config_data = load_config(config)
    input_path = Path(input_file)
    output_path = Path(output_file)

    if not input_path.exists():
        raise FileNotFoundError(f"Input file does not exist: {input_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    command = build_ffmpeg_command(input_path, output_path, config_data)
    start = time.perf_counter()
    LOGGER.info("audio_extractor:start input=%s output=%s", input_path, output_path)

    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        raise RuntimeError(
            f"FFmpeg failed while processing {input_path}: {stderr}"
        ) from exc

    duration = time.perf_counter() - start
    LOGGER.info(
        "audio_extractor:end output=%s duration_seconds=%.2f",
        output_path,
        duration,
    )
    return output_path


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract and normalize audio into a 44.1 kHz WAV file.",
    )
    parser.add_argument("input_file", help="Source media file, e.g. input/test.mp3")
    parser.add_argument(
        "output_file",
        help="Destination WAV file, e.g. temp/audio.wav",
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
    extract_and_normalize_audio(args.input_file, args.output_file, args.config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
