"""Transcribe audio locally with a configurable speech-to-text provider.

This stage defaults to Whisper. Optionally, it can call a Gemma-based
transcriber and fall back to Whisper if the optional autosync stack is missing
or the Gemma run fails.
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


def _load_gemma_transcriber():
    try:
        from modules.gemma_transcriber import transcribe_audio_with_gemma
    except ImportError as exc:  # pragma: no cover - depends on optional environment
        raise ModuleNotFoundError(
            "Gemma transcription requires the optional autosync dependencies. "
            "Run scripts/setup_autosync.ps1 to install them."
        ) from exc
    return transcribe_audio_with_gemma


def _is_cuda_available() -> bool:
    try:
        import torch  # type: ignore
    except Exception:
        return False
    return bool(torch.cuda.is_available())


def build_transcriber_settings(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return the normalized transcriber settings from config."""
    settings = _transcriber_section(config)
    autosync = config.get("autosync")
    if not isinstance(autosync, Mapping):
        autosync = {}

    provider = str(settings.get("provider") or "").strip().lower()
    if not provider:
        autosync_provider = str(autosync.get("provider", "")).strip().lower()
        autosync_enabled = bool(autosync.get("enabled", False))
        provider = autosync_provider if autosync_enabled and autosync_provider else "whisper"

    return {
        "provider": provider or "whisper",
        "fallback_to_whisper": bool(
            settings.get("fallback_to_whisper", autosync.get("fallback_to_whisper", True))
        ),
        "model_name": str(settings["model_name"]),
        "device": str(settings.get("device", "cpu")),
        "language": str(settings.get("language", autosync.get("language", "he"))),
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


def _transcribe_with_whisper(
    input_path: Path,
    settings: Mapping[str, Any],
    json_path: Path,
    text_path: Path,
) -> dict[str, Any]:
    from faster_whisper import WhisperModel
    start = time.perf_counter()
    LOGGER.info(
        "transcriber:start provider=whisper input=%s model=%s",
        input_path,
        settings["model_name"],
    )
    
    model = WhisperModel(
        settings["model_name"],
        device=settings["device"],
        compute_type="int8" if settings["device"] == "cpu" else "float16",
    )
    segments, info = model.transcribe(
        str(input_path),
        language=settings["language"],
        task=settings["task"],
    )
    
    # Convert segments generator to list
    segments_list = list(segments)
    full_text = " ".join([s.text for s in segments_list])
    
    # Normalize segments to match the expected format
    normalized_segments = [
        {
            "id": i,
            "start": s.start,
            "end": s.end,
            "text": s.text.strip(),
        }
        for i, s in enumerate(segments_list)
    ]
    
    transcript = {
        "source_file": str(input_path),
        "provider": "whisper",
        "model_name": settings["model_name"],
        "language": info.language,
        "text": full_text,
        "segments": normalized_segments,
    }
    _write_transcript_files(transcript, json_path, text_path)
    
    duration = time.perf_counter() - start
    LOGGER.info(
        "transcriber:end provider=whisper json=%s text=%s duration_seconds=%.2f",
        json_path,
        text_path,
        duration,
    )
    transcript["json_path"] = str(json_path)
    transcript["text_path"] = str(text_path)
    return transcript



def transcribe_audio(
    input_file: str | Path,
    config: str | Path | Mapping[str, Any],
) -> dict[str, Any]:
    """Transcribe audio and save transcript artifacts under temp/."""
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

    provider = str(settings.get("provider", "whisper")).strip().lower()
    if provider == "gemma":
        transcribe_audio_with_gemma = _load_gemma_transcriber()
        try:
            return transcribe_audio_with_gemma(input_path, config_data)
        except Exception as exc:
            if not bool(settings.get("fallback_to_whisper", True)):
                raise
            fallback_device = str(settings.get("device", "cpu")).strip().lower()
            if fallback_device == "cpu" and _is_cuda_available():
                fallback_device = "cuda"
            LOGGER.warning(
                "transcriber:gemma_fallback input=%s reason=%s: %s | action=install optional autosync deps (scripts/setup_autosync.ps1) | fallback_provider=whisper device=%s",
                input_path,
                type(exc).__name__,
                exc,
                fallback_device,
            )
            settings = dict(settings)
            settings["device"] = fallback_device
            if fallback_device.startswith("cuda"):
                settings["fp16"] = False

    return _transcribe_with_whisper(input_path, settings, json_path, text_path)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Transcribe audio with a local Whisper model or Gemma provider.",
    )
    parser.add_argument("input_file", help="Input audio file, e.g. temp/vocals.wav")
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
