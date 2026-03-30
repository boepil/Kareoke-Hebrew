"""Separate vocals from accompaniment using Demucs.

This stage runs the local Demucs CLI, then copies the generated stems into the
pipeline's stage files:
- temp/vocals.wav
- temp/no_vocals.wav
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
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
) -> dict[str, Path]:
    """Run Demucs and stage the separated stems into temp/."""
    config_data = load_config(config)
    input_path = Path(input_file)

    if not input_path.exists():
        raise FileNotFoundError(f"Input file does not exist: {input_path}")

    paths = _paths_section(config_data)
    temp_dir = Path(paths["temp_dir"])
    temp_dir.mkdir(parents=True, exist_ok=True)

    command = build_demucs_command(input_path, temp_dir, config_data)
    start = time.perf_counter()
    LOGGER.info("separator:start input=%s temp_dir=%s", input_path, temp_dir)

    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        raise RuntimeError(
            f"Demucs failed while processing {input_path}: {stderr}"
        ) from exc

    demucs_song_dir = expected_demucs_song_dir(input_path, temp_dir, config_data)
    outputs = finalize_separation_outputs(demucs_song_dir, temp_dir)

    duration = time.perf_counter() - start
    LOGGER.info(
        "separator:end vocals=%s no_vocals=%s duration_seconds=%.2f",
        outputs["vocals"],
        outputs["no_vocals"],
        duration,
    )
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
