"""Gemma-backed audio transcription for the karaoke pipeline.

This provider is intended for the first-pass autosync path. It chunks the
input vocals WAV, prompts a local Gemma multimodal model with audio plus text
instructions, and writes a coarse segment transcript that can later be refined
by lyric correction and WhisperX alignment.
"""

from __future__ import annotations

import json
import inspect
import logging
import time
from pathlib import Path
from typing import Any, Mapping

import yaml

LOGGER = logging.getLogger(__name__)

DEFAULT_GEMMA_MODEL_ID = "google/gemma-3n-E4B-it"
DEFAULT_PROMPT = (
    "Transcribe this Hebrew singing audio verbatim in Hebrew script only. "
    "Do not translate. Keep artist chatter out. Return only the sung words."
)


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


def build_gemma_transcriber_settings(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return the normalized Gemma settings from config."""
    settings = _transcriber_section(config)
    autosync = config.get("autosync")
    if not isinstance(autosync, Mapping):
        autosync = {}

    chunk_seconds = float(settings.get("gemma_chunk_seconds", autosync.get("chunk_seconds", 24.0)))
    overlap_seconds = float(
        settings.get("gemma_chunk_overlap_seconds", autosync.get("chunk_overlap_seconds", 1.0))
    )
    if chunk_seconds <= 0:
        raise ValueError("transcriber.gemma_chunk_seconds must be > 0")
    if overlap_seconds < 0:
        raise ValueError("transcriber.gemma_chunk_overlap_seconds must be >= 0")
    if overlap_seconds >= chunk_seconds:
        raise ValueError("gemma chunk overlap must be smaller than the chunk length")

    return {
        "model_id": str(
            settings.get("gemma_model_id")
            or autosync.get("audio_model_id")
            or DEFAULT_GEMMA_MODEL_ID
        ),
        "device_map": str(settings.get("gemma_device_map", "auto")),
        "torch_dtype": str(settings.get("gemma_torch_dtype", "auto")),
        "language": str(settings.get("language", autosync.get("language", "he"))),
        "chunk_seconds": chunk_seconds,
        "chunk_overlap_seconds": overlap_seconds,
        "sample_rate_hz": int(settings.get("gemma_sample_rate_hz", 16000)),
        "max_new_tokens": int(settings.get("gemma_max_new_tokens", 196)),
        "instruction_text": str(settings.get("gemma_instruction_text", DEFAULT_PROMPT)).strip() or DEFAULT_PROMPT,
        "output_json_name": str(settings.get("output_json_name", "transcript.json")),
        "output_text_name": str(settings.get("output_text_name", "transcript.txt")),
    }


def _load_audio_runtime():
    try:
        import librosa  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on optional environment
        raise ModuleNotFoundError(
            "Gemma transcription requires 'librosa'. "
            "Run scripts/setup_autosync.ps1 to install the optional autosync stack."
        ) from exc
    return librosa


def _load_gemma_runtime():
    try:
        import torch  # type: ignore
        from transformers import AutoModelForImageTextToText, AutoProcessor  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on optional environment
        raise ModuleNotFoundError(
            "Gemma transcription requires 'torch' and 'transformers'. "
            "Run scripts/setup_autosync.ps1 to install the optional autosync stack."
        ) from exc
    return torch, AutoModelForImageTextToText, AutoProcessor


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


def _build_chunk_plan(total_seconds: float, chunk_seconds: float, overlap_seconds: float) -> list[tuple[float, float]]:
    if total_seconds <= 0:
        return [(0.0, chunk_seconds)]

    plan: list[tuple[float, float]] = []
    step_seconds = max(chunk_seconds - overlap_seconds, 0.1)
    start_seconds = 0.0
    while start_seconds < total_seconds:
        end_seconds = min(start_seconds + chunk_seconds, total_seconds)
        plan.append((start_seconds, end_seconds))
        if end_seconds >= total_seconds:
            break
        start_seconds += step_seconds
    return plan


def _transcribe_chunk(
    chunk_audio: Any,
    sample_rate: int,
    settings: Mapping[str, Any],
    processor: Any,
    model: Any,
) -> str:
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "audio",
                    "audio": chunk_audio,
                },
                {"type": "text", "text": settings["instruction_text"]},
            ],
        }
    ]
    inputs = processor.apply_chat_template(
        messages,
        add_generation_prompt=True,
        tokenize=True,
        return_dict=True,
        return_tensors="pt",
    )

    if hasattr(inputs, "to"):
        model_dtype = getattr(model, "dtype", None)
        if model_dtype is not None:
            inputs = inputs.to(model.device, dtype=model_dtype)
        else:
            inputs = inputs.to(model.device)

    output_ids = model.generate(
        **inputs,
        max_new_tokens=int(settings["max_new_tokens"]),
    )
    prompt_width = 0
    if isinstance(inputs, Mapping) and "input_ids" in inputs:
        prompt_width = int(inputs["input_ids"].shape[-1])
    generated_ids = output_ids[:, prompt_width:] if prompt_width else output_ids
    decoded = processor.batch_decode(
        generated_ids,
        skip_special_tokens=True,
        clean_up_tokenization_spaces=False,
    )
    return str(decoded[0] if decoded else "").strip()


def _resolve_dtype_arg(torch: Any, raw_dtype: str) -> Any:
    if raw_dtype == "auto":
        return "auto"
    return getattr(torch, raw_dtype)


def _build_model_load_kwargs(
    torch: Any,
    model_loader: Any,
    settings: Mapping[str, Any],
) -> dict[str, Any]:
    dtype_value = _resolve_dtype_arg(torch, str(settings["torch_dtype"]))
    kwargs: dict[str, Any] = {
        "device_map": settings["device_map"],
    }
    try:
        parameters = inspect.signature(model_loader.from_pretrained).parameters
        if "dtype" in parameters:
            kwargs["dtype"] = dtype_value
        else:
            kwargs["torch_dtype"] = dtype_value
    except (TypeError, ValueError):
        kwargs["torch_dtype"] = dtype_value
    return kwargs


def transcribe_audio_with_gemma(
    input_file: str | Path,
    config: str | Path | Mapping[str, Any],
) -> dict[str, Any]:
    """Transcribe audio with a local Gemma multimodal model."""
    config_data = load_config(config)
    input_path = Path(input_file)
    if not input_path.exists():
        raise FileNotFoundError(f"Input file does not exist: {input_path}")

    paths = _paths_section(config_data)
    settings = build_gemma_transcriber_settings(config_data)
    temp_dir = Path(paths["temp_dir"])
    temp_dir.mkdir(parents=True, exist_ok=True)

    json_path = temp_dir / settings["output_json_name"]
    text_path = temp_dir / settings["output_text_name"]

    librosa = _load_audio_runtime()
    torch, AutoModelForImageTextToText, AutoProcessor = _load_gemma_runtime()

    start = time.perf_counter()
    LOGGER.info(
        "transcriber:start provider=gemma input=%s model=%s",
        input_path,
        settings["model_id"],
    )

    waveform, sample_rate = librosa.load(
        str(input_path),
        sr=int(settings["sample_rate_hz"]),
        mono=True,
    )
    total_seconds = float(len(waveform)) / float(sample_rate) if sample_rate else 0.0
    chunk_plan = _build_chunk_plan(
        total_seconds=total_seconds,
        chunk_seconds=float(settings["chunk_seconds"]),
        overlap_seconds=float(settings["chunk_overlap_seconds"]),
    )

    processor = AutoProcessor.from_pretrained(settings["model_id"])
    model = AutoModelForImageTextToText.from_pretrained(
        settings["model_id"],
        **_build_model_load_kwargs(torch, AutoModelForImageTextToText, settings),
    )

    transcript_segments: list[dict[str, Any]] = []
    transcript_parts: list[str] = []

    for chunk_index, (start_seconds, end_seconds) in enumerate(chunk_plan):
        start_frame = int(start_seconds * sample_rate)
        end_frame = max(int(end_seconds * sample_rate), start_frame + 1)
        chunk_audio = waveform[start_frame:end_frame]
        chunk_text = _transcribe_chunk(chunk_audio, sample_rate, settings, processor, model).strip()
        if not chunk_text:
            continue
        transcript_parts.append(chunk_text)
        transcript_segments.append(
            {
                "id": chunk_index,
                "start": round(start_seconds, 3),
                "end": round(end_seconds, 3),
                "text": chunk_text,
            }
        )

    transcript = {
        "source_file": str(input_path),
        "provider": "gemma",
        "model_name": settings["model_id"],
        "language": settings["language"],
        "text": "\n".join(transcript_parts).strip(),
        "segments": transcript_segments,
    }
    _write_transcript_files(transcript, json_path, text_path)

    duration = time.perf_counter() - start
    LOGGER.info(
        "transcriber:end provider=gemma json=%s text=%s duration_seconds=%.2f",
        json_path,
        text_path,
        duration,
    )
    transcript["json_path"] = str(json_path)
    transcript["text_path"] = str(text_path)
    return transcript
