"""Separate vocals from accompaniment using Demucs.

This stage runs the local Demucs CLI, then copies the generated stems into the
pipeline's stage files:
- temp/vocals.wav
- temp/no_vocals.wav
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

LOGGER = logging.getLogger(__name__)


class SeparationCancelledError(RuntimeError):
    """Raised when the user cancels a running Demucs separation."""


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
    runtime_data_root = os.environ.get("KARAOKE_USER_DATA_DIR", "").strip()
    if not runtime_data_root:
        return paths
    runtime_paths = dict(paths)
    runtime_paths["temp_dir"] = str(Path(runtime_data_root).expanduser().resolve() / "temp")
    return runtime_paths


def _separator_section(config: Mapping[str, Any]) -> Mapping[str, Any]:
    settings = config.get("separator")
    if not isinstance(settings, Mapping):
        raise KeyError("Missing 'separator' section in config")
    return settings


def build_demucs_command(
    input_file: str | Path,
    output_root: str | Path,
    config: Mapping[str, Any],
) -> list[str]:
    """Build the Demucs CLI command used for vocal separation."""
    settings = _separator_section(config)

    module_name = str(settings["demucs_module"])
    model_name = str(settings["model_name"])
    two_stems = str(settings.get("two_stems", "vocals"))
    device = str(settings.get("device", "cpu"))
    shifts = int(settings.get("shifts", 0))

    command = [
        sys.executable,
        "-m",
        module_name,
        "-n",
        model_name,
        "--two-stems",
        two_stems,
        "-o",
        str(output_root),
        "--device",
        device,
    ]

    if shifts:
        command.extend(["--shifts", str(shifts)])

    command.append(str(input_file))
    return command


def expected_demucs_song_dir(
    input_file: str | Path,
    output_root: str | Path,
    config: Mapping[str, Any],
) -> Path:
    """Return the expected Demucs output directory for the input track."""
    settings = _separator_section(config)
    model_name = str(settings["model_name"])
    input_path = Path(input_file)
    return Path(output_root) / model_name / input_path.stem


def finalize_separation_outputs(
    demucs_song_dir: str | Path,
    temp_dir: str | Path,
) -> dict[str, Path]:
    """Copy Demucs output stems into the stage filenames under temp/."""
    song_dir = Path(demucs_song_dir)
    temp_path = Path(temp_dir)

    vocals_source = song_dir / "vocals.wav"
    no_vocals_source = song_dir / "no_vocals.wav"
    if not vocals_source.exists():
        raise FileNotFoundError(f"Missing Demucs vocals stem: {vocals_source}")
    if not no_vocals_source.exists():
        raise FileNotFoundError(f"Missing Demucs accompaniment stem: {no_vocals_source}")

    temp_path.mkdir(parents=True, exist_ok=True)
    vocals_target = temp_path / "vocals.wav"
    no_vocals_target = temp_path / "no_vocals.wav"
    shutil.copy2(vocals_source, vocals_target)
    shutil.copy2(no_vocals_source, no_vocals_target)
    return {"vocals": vocals_target, "no_vocals": no_vocals_target}


def separate_vocals(
    input_file: str | Path,
    config: str | Path | Mapping[str, Any],
    progress_callback: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, Path]:
    """Run Demucs in-process and stage the separated stems into temp/."""
    config_data = load_config(config)
    input_path = Path(input_file)

    if not input_path.exists():
        raise FileNotFoundError(f"Input file does not exist: {input_path}")

    paths = _paths_section(config_data)
    temp_dir = Path(paths["temp_dir"])
    temp_dir.mkdir(parents=True, exist_ok=True)

    settings = _separator_section(config_data)
    model_name = str(settings["model_name"])
    two_stems = str(settings.get("two_stems", "vocals"))
    device = str(settings.get("device", "cpu"))
    shifts = int(settings.get("shifts", 0))
    start = time.perf_counter()
    LOGGER.info("separator:start input=%s temp_dir=%s", input_path, temp_dir)

    def raise_if_cancelled() -> None:
        if cancel_check is not None and cancel_check():
            raise SeparationCancelledError("Stem separation cancelled")

    try:
        raise_if_cancelled()
        from demucs.api import Separator, save_audio

        separator = Separator(
            model=model_name,
            device=device,
            shifts=shifts,
            progress=False,
            callback=None,
        )
        # Configure the callback after loading the model, so we can calculate the
        # actual chunk count and report useful progress without console output.
        model_count = len(getattr(separator.model, "models", ())) or 1
        segment_seconds = getattr(
            getattr(separator.model, "models", [separator.model])[0], "segment", None
        )
        overlap = 0.25
        if isinstance(segment_seconds, (int, float)) and segment_seconds > 0:
            stride = max(1, int(segment_seconds * separator.samplerate * (1 - overlap)))
        else:
            stride = max(1, separator.samplerate * 7)
        completed_chunks: set[tuple[int, int]] = set()

        def on_progress(event: Mapping[str, Any]) -> None:
            raise_if_cancelled()
            if event.get("state") != "end":
                return
            model_index = int(event.get("model_idx_in_bag", 0))
            shift_index = int(event.get("shift_idx", 0))
            offset = int(event.get("segment_offset", 0))
            completed_chunks.add((model_index * max(1, shifts + 1) + shift_index, offset))
            audio_length = max(1, int(event.get("audio_length", 1)))
            chunks_per_pass = max(1, math.ceil(audio_length / stride))
            total_chunks = model_count * max(1, shifts + 1) * chunks_per_pass
            percent = min(99, round(100 * len(completed_chunks) / total_chunks))
            if progress_callback:
                progress_callback(f"Separating stems: {percent}% ({model_index + 1}/{model_count} models)")

        separator.update_parameter(callback=on_progress)
        raise_if_cancelled()
        _, stems = separator.separate_audio_file(input_path)
        raise_if_cancelled()
        if two_stems not in stems:
            raise RuntimeError(f"Demucs did not produce the requested '{two_stems}' stem")
        selected = stems[two_stems]
        remaining = [stem for name, stem in stems.items() if name != two_stems]
        if not remaining:
            raise RuntimeError("Demucs did not produce the accompaniment stem")
        accompaniment = remaining[0].clone()
        for stem in remaining[1:]:
            accompaniment += stem

        temp_dir.mkdir(parents=True, exist_ok=True)
        vocals_target = temp_dir / "vocals.wav"
        no_vocals_target = temp_dir / "no_vocals.wav"
        save_audio(selected, vocals_target, samplerate=separator.samplerate,
                   clip="rescale", bits_per_sample=16)
        save_audio(accompaniment, no_vocals_target, samplerate=separator.samplerate,
                   clip="rescale", bits_per_sample=16)
        outputs = {"vocals": vocals_target, "no_vocals": no_vocals_target}
    except Exception as exc:
        if isinstance(exc, SeparationCancelledError):
            raise
        LOGGER.exception("Demucs failed while processing %s", input_path)
        raise RuntimeError(f"Demucs failed while processing {input_path}: {exc}") from exc

    duration = time.perf_counter() - start
    LOGGER.info(
        "separator:end vocals=%s no_vocals=%s duration_seconds=%.2f",
        outputs["vocals"],
        outputs["no_vocals"],
        duration,
    )
    if progress_callback:
        progress_callback("Vocal and accompaniment stems are ready")
    return outputs


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Separate vocals and accompaniment with Demucs.",
    )
    parser.add_argument("input_file", help="Source WAV file, e.g. temp/audio.wav")
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml (default: config.yaml)",
    )
    return parser


def main() -> int:
    parser = _build_arg_parser()
    args = parser.parse_args()
    separate_vocals(args.input_file, args.config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
