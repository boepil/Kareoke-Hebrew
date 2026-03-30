"""Render the final karaoke MP4.

This MVP renderer creates a black video canvas, burns the ASS subtitles into
it, and muxes the accompaniment audio into the final MP4 output.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import time
import wave
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

LOGGER = logging.getLogger(__name__)


def _audio_duration_seconds(path: Path) -> float:
    if path.suffix.lower() != ".wav":
        return 0.0
    with wave.open(str(path), "rb") as wav_file:
        frame_rate = wav_file.getframerate()
        total_frames = wav_file.getnframes()
    if frame_rate <= 0:
        return 0.0
    return total_frames / frame_rate


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


def _renderer_section(config: Mapping[str, Any]) -> Mapping[str, Any]:
    settings = config.get("renderer")
    if not isinstance(settings, Mapping):
        raise KeyError("Missing 'renderer' section in config")
    return settings


def _subtitle_builder_section(config: Mapping[str, Any]) -> Mapping[str, Any]:
    settings = config.get("subtitle_builder")
    if not isinstance(settings, Mapping):
        return {}
    return settings


def build_render_command(
    no_vocals_audio: str | Path,
    subtitles_file: str | Path,
    output_video: str | Path,
    config: Mapping[str, Any],
    duration_seconds: float | None = None,
) -> list[str]:
    """Build the FFmpeg command used to render the karaoke MP4."""
    settings = _renderer_section(config)

    ffmpeg_path = str(settings["ffmpeg_path"])
    output_video_name = str(settings.get("output_video_name", "karaoke.mp4"))
    video_size = str(settings.get("video_size", "1920x1080"))
    frame_rate = str(settings.get("frame_rate", 30))
    background_color = str(settings.get("background_color", "black"))
    video_codec = str(settings.get("video_codec", "libx264"))
    video_preset = str(settings.get("video_preset", "medium"))
    video_crf = str(settings.get("video_crf", 18))
    audio_codec = str(settings.get("audio_codec", "aac"))
    audio_bitrate = str(settings.get("audio_bitrate", "192k"))
    faststart = bool(settings.get("faststart", True))

    subtitles_path = Path(subtitles_file).resolve().as_posix()
    subtitles_path = subtitles_path.replace(":", r"\:")
    subtitles_filter = f"ass='{subtitles_path}'"

    command = [
        ffmpeg_path,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"color=c={background_color}:s={video_size}:r={frame_rate}",
        "-i",
        str(no_vocals_audio),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-vf",
        subtitles_filter,
        "-shortest",
        "-c:v",
        video_codec,
        "-preset",
        video_preset,
        "-crf",
        video_crf,
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        audio_codec,
        "-b:a",
        audio_bitrate,
    ]

    if faststart:
        command.extend(["-movflags", "+faststart"])

    if duration_seconds and duration_seconds > 0:
        command.extend(["-t", f"{duration_seconds:.3f}"])

    command.append(str(output_video))
    return command


def _load_image_manifest(manifest_path: Path) -> list[dict[str, Any]]:
    with manifest_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    events = payload.get("events", [])
    if not isinstance(events, list):
        raise ValueError(f"Subtitle manifest must contain an 'events' list: {manifest_path}")
    normalized_events: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        image_path = Path(str(event.get("image", "")))
        if not image_path.exists():
            continue
        normalized_events.append(
            {
                "start": float(event["start"]),
                "end": float(event["end"]),
                "image": image_path,
            }
        )
    return normalized_events


def _build_image_overlay_filter_script(
    events: list[dict[str, Any]],
    script_path: Path,
    y_expression: str,
) -> tuple[list[str], str]:
    input_args: list[str] = []
    filter_lines: list[str] = []
    previous_label = "[0:v]"

    for index, event in enumerate(events, start=2):
        image_path = Path(event["image"]).resolve()
        input_args.extend(["-loop", "1", "-i", str(image_path)])
        next_label = f"[v{index}]"
        start = float(event["start"])
        end = float(event["end"])
        filter_lines.append(
            f"{previous_label}[{index}:v]overlay=x=(W-w)/2:y={y_expression}:enable='between(t,{start:.3f},{end:.3f})'{next_label}"
        )
        previous_label = next_label

    filter_lines.append(f"{previous_label}copy[vout]")
    script_path.write_text(";\n".join(filter_lines), encoding="utf-8")
    return input_args, "[vout]"


def render_video(
    no_vocals_audio: str | Path,
    subtitles_file: str | Path,
    config: str | Path | Mapping[str, Any],
    progress_callback: Callable[[float, str], None] | None = None,
) -> Path:
    """Render the final karaoke video into output/."""
    config_data = load_config(config)
    audio_path = Path(no_vocals_audio)
    subtitles_path = Path(subtitles_file)

    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file does not exist: {audio_path}")
    if not subtitles_path.exists():
        raise FileNotFoundError(f"Subtitle file does not exist: {subtitles_path}")

    paths = _paths_section(config_data)
    settings = _renderer_section(config_data)
    subtitle_settings = _subtitle_builder_section(config_data)
    output_dir = Path(paths["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    output_video = output_dir / str(settings.get("output_video_name", "karaoke.mp4"))
    manifest_path = subtitles_path.with_name(str(subtitle_settings.get("manifest_name", "subtitles_manifest.json")))
    total_duration = max(_audio_duration_seconds(audio_path), 0.0)
    start = time.perf_counter()
    LOGGER.info(
        "renderer:start audio=%s subtitles=%s output=%s",
        audio_path,
        subtitles_path,
        output_video,
    )

    if manifest_path.exists():
        events = _load_image_manifest(manifest_path)
        if not events:
            raise RuntimeError(f"Subtitle manifest has no usable events: {manifest_path}")

        ffmpeg_path = str(settings["ffmpeg_path"])
        video_size = str(settings.get("video_size", "1920x1080"))
        frame_rate = str(settings.get("frame_rate", 30))
        background_color = str(settings.get("background_color", "black"))
        video_codec = str(settings.get("video_codec", "libx264"))
        video_preset = str(settings.get("video_preset", "medium"))
        video_crf = str(settings.get("video_crf", 18))
        audio_codec = str(settings.get("audio_codec", "aac"))
        audio_bitrate = str(settings.get("audio_bitrate", "192k"))
        faststart = bool(settings.get("faststart", True))
        margin_v = int(subtitle_settings.get("margin_v", 28))
        overlay_vertical_align = str(subtitle_settings.get("overlay_vertical_align", "center")).strip().lower()
        y_expression = "(H-h)/2" if overlay_vertical_align == "center" else f"H-h-{margin_v}"

        script_path = subtitles_path.with_name("subtitle_overlay.ffscript")
        image_inputs, video_label = _build_image_overlay_filter_script(events, script_path, y_expression)
        command = [
            ffmpeg_path,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c={background_color}:s={video_size}:r={frame_rate}",
            "-i",
            str(audio_path),
            *image_inputs,
            "-filter_complex_script",
            str(script_path),
            "-map",
            video_label,
            "-map",
            "1:a:0",
            "-shortest",
            "-c:v",
            video_codec,
            "-preset",
            video_preset,
            "-crf",
            video_crf,
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            audio_codec,
            "-b:a",
            audio_bitrate,
        ]
        if faststart:
            command.extend(["-movflags", "+faststart"])
        if total_duration > 0:
            command.extend(["-t", f"{total_duration:.3f}"])
        command.append(str(output_video))
    else:
        command = build_render_command(audio_path, subtitles_path, output_video, config_data, total_duration)

    command[1:1] = ["-progress", "pipe:1", "-nostats"]

    def parse_progress_value(raw_value: str) -> float | None:
        cleaned = raw_value.strip()
        if not cleaned or cleaned.upper() == "N/A":
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        log_lines: list[str] = []
        if process.stdout is not None:
            for raw_line in process.stdout:
                line = raw_line.strip()
                if line:
                    log_lines.append(line)
                if progress_callback is None:
                    continue
                if line.startswith("out_time_ms="):
                    out_time_ms = parse_progress_value(line.split("=", 1)[1])
                    if out_time_ms is not None and total_duration > 0:
                        progress_callback(min(max(out_time_ms / 1_000_000 / total_duration, 0.0), 0.999), "rendering")
                elif line.startswith("out_time_us="):
                    out_time_us = parse_progress_value(line.split("=", 1)[1])
                    if out_time_us is not None and total_duration > 0:
                        progress_callback(min(max(out_time_us / 1_000_000 / total_duration, 0.0), 0.999), "rendering")
                elif line == "progress=end":
                    progress_callback(1.0, "finalizing")

        return_code = process.wait()
        if return_code != 0:
            stderr = "\n".join(log_lines[-20:]).strip()
            raise subprocess.CalledProcessError(return_code, command, output="\n".join(log_lines), stderr=stderr)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        raise RuntimeError(
            f"FFmpeg rendering failed for {audio_path}: {stderr}"
        ) from exc

    duration = time.perf_counter() - start
    LOGGER.info(
        "renderer:end output=%s duration_seconds=%.2f",
        output_video,
        duration,
    )
    return output_video


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render the final karaoke MP4 from audio and ASS subtitles.",
    )
    parser.add_argument("no_vocals_audio", help="Input accompaniment audio, e.g. temp/no_vocals.wav")
    parser.add_argument("subtitles_file", help="Input ASS subtitles, e.g. temp/subtitles.ass")
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml (default: config.yaml)",
    )
    return parser


def main() -> int:
    parser = _build_arg_parser()
    args = parser.parse_args()
    render_video(args.no_vocals_audio, args.subtitles_file, args.config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
