"""Render the final karaoke MP4.

This MVP renderer creates a black video canvas, burns the ASS subtitles into
it, and muxes the accompaniment audio into the final MP4 output.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import time
import wave
from pathlib import Path
from typing import Any, Callable, Mapping

from PIL import Image, ImageDraw, ImageFont

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


def _find_font_path(font_name: str, font_path: str) -> str | None:
    if font_path:
        candidate = Path(font_path)
        if candidate.exists():
            return str(candidate)

    direct_candidate = Path(font_name)
    if direct_candidate.exists():
        return str(direct_candidate)

    windows_fonts = Path("C:/Windows/Fonts")
    candidate_files = (
        font_name,
        f"{font_name}.ttf",
        f"{font_name}.ttc",
        "micross.ttf",
        "arialuni.ttf",
        "arial.ttf",
        "David.ttf",
        "DavidLibre-Regular.ttf",
    )
    for candidate_name in candidate_files:
        candidate = windows_fonts / candidate_name
        if candidate.exists():
            return str(candidate)
    return None


def _load_font(font_name: str, font_path: str, font_size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    resolved_path = _find_font_path(font_name, font_path)
    if resolved_path:
        try:
            return ImageFont.truetype(resolved_path, font_size)
        except OSError:
            pass
    try:
        return ImageFont.truetype(font_name, font_size)
    except OSError:
        return ImageFont.load_default()


def _measure_font_width(font: ImageFont.FreeTypeFont | ImageFont.ImageFont, text: str) -> float:
    if hasattr(font, "getlength"):
        return float(font.getlength(text))
    bbox = font.getbbox(text)
    return float(bbox[2] - bbox[0])


def _contains_hebrew(text: str) -> bool:
    return bool(re.search(r"[\u0590-\u05FF]", text))


def _draw_centered_line(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    center_x: int,
    y: int,
    fill: tuple[int, int, int, int],
    stroke_fill: tuple[int, int, int, int],
    stroke_width: int,
) -> None:
    if not text:
        return

    if not _contains_hebrew(text):
        x = center_x - int(_measure_font_width(font, text) / 2)
        draw.text(
            (x, y),
            text,
            font=font,
            fill=fill,
            stroke_width=stroke_width,
            stroke_fill=stroke_fill,
        )
        return

    words = [token for token in text.split(" ") if token]
    if not words:
        return
    display_words = [word[::-1] for word in words]
    space_width = _measure_font_width(font, " ")
    word_widths = [_measure_font_width(font, word) for word in display_words]
    total_width = sum(word_widths) + space_width * max(len(display_words) - 1, 0)
    x = center_x + int(total_width / 2)

    for index, word in enumerate(display_words):
        word_width = word_widths[index]
        x -= int(word_width)
        draw.text(
            (x, y),
            word,
            font=font,
            fill=fill,
            stroke_width=stroke_width,
            stroke_fill=stroke_fill,
        )
        if index < len(display_words) - 1:
            x -= int(space_width)


def _load_manifest_payload(manifest_path: Path) -> dict[str, Any]:
    with manifest_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"Subtitle manifest must contain an object: {manifest_path}")
    return payload


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
    payload = _load_manifest_payload(manifest_path)
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
                "fade_in_seconds": max(float(event.get("fade_in_seconds", 0.0) or 0.0), 0.0),
                "fade_out_seconds": max(float(event.get("fade_out_seconds", 0.0) or 0.0), 0.0),
                "kind": str(event.get("kind", "")).strip(),
                "text": str(event.get("text", "")).strip(),
            }
        )
    return normalized_events


def _render_intro_card(
    output_path: Path,
    title: str,
    subtitle: str,
    settings: Mapping[str, Any],
    video_size: tuple[int, int],
) -> None:
    width, height = video_size
    font_name = str(settings.get("font_name", "Microsoft Sans Serif"))
    font_path = str(settings.get("font_path", ""))
    base_size = max(int(settings.get("font_size", 56)), 1)
    title_size = max(int(base_size * 2), 24)
    subtitle_size = max(int(base_size * 0.75), 18)

    max_text_width = int(width * 0.78)
    min_size = max(int(base_size * 1.2), 24)
    title_font = _load_font(font_name, font_path, title_size)
    while title_size > min_size and _measure_font_width(title_font, title) > max_text_width:
        title_size -= 2
        title_font = _load_font(font_name, font_path, title_size)

    subtitle_font = None
    if subtitle:
        subtitle_font = _load_font(font_name, font_path, subtitle_size)
        while subtitle_size > 16 and _measure_font_width(subtitle_font, subtitle) > max_text_width:
            subtitle_size -= 1
            subtitle_font = _load_font(font_name, font_path, subtitle_size)

    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    block_left = int(width * 0.11)
    block_right = int(width * 0.89)
    block_top = int(height * 0.08)
    padding_x = int(width * 0.03)
    padding_y = int(height * 0.02)
    title_bbox = title_font.getbbox(title)
    title_height = title_bbox[3] - title_bbox[1]
    subtitle_height = 0
    if subtitle and subtitle_font is not None:
        subtitle_bbox = subtitle_font.getbbox(subtitle)
        subtitle_height = subtitle_bbox[3] - subtitle_bbox[1]
    block_height = title_height + subtitle_height + padding_y * 4 + (12 if subtitle_height else 0)
    block_bottom = min(height - int(height * 0.12), block_top + block_height)

    draw.rounded_rectangle(
        (block_left, block_top, block_right, block_bottom),
        radius=32,
        fill=(15, 18, 24, 225),
        outline=(255, 255, 255, 18),
        width=1,
    )

    center_x = width // 2
    current_y = block_top + padding_y * 2
    title_fill = (245, 245, 245, 255)
    outline_fill = (0, 0, 0, 255)
    _draw_centered_line(
        draw,
        title,
        title_font,
        center_x,
        current_y,
        title_fill,
        outline_fill,
        3,
    )
    current_y += title_height + 12

    if subtitle and subtitle_font is not None:
        _draw_centered_line(
            draw,
            subtitle,
            subtitle_font,
            center_x,
            current_y,
            (190, 190, 190, 255),
            outline_fill,
            2,
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path)


def _build_image_overlay_filter_script(
    events: list[dict[str, Any]],
    script_path: Path,
    y_expression: str,
    fade_seconds: float = 0.5,
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
        overlay_start = max(0.0, start)
        event_fade_seconds = max(float(event.get("fade_in_seconds", fade_seconds) or 0.0), 0.0)
        event_fade_out_seconds = max(float(event.get("fade_out_seconds", 0.0) or 0.0), 0.0)
        fade_label = f"[f{index}]"
        fade_filters: list[str] = []
        if event_fade_seconds > 0:
            fade_filters.append(f"fade=t=in:st={overlay_start:.3f}:d={event_fade_seconds:.3f}:alpha=1")
        if event_fade_out_seconds > 0:
            fade_out_start = max(end - event_fade_out_seconds, overlay_start)
            fade_filters.append(f"fade=t=out:st={fade_out_start:.3f}:d={event_fade_out_seconds:.3f}:alpha=1")
        fade_suffix = f",{','.join(fade_filters)}" if fade_filters else ""
        filter_lines.append(
            f"[{index}:v]format=rgba,trim=end={end:.3f},setpts=PTS-STARTPTS+{overlay_start:.3f}/TB{fade_suffix}{fade_label}"
        )
        filter_lines.append(
            f"{previous_label}{fade_label}overlay=x=(W-w)/2:y={y_expression}:enable='between(t,{overlay_start:.3f},{end:.3f})'{next_label}"
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
        payload = _load_manifest_payload(manifest_path)
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
        video_width, video_height = (int(part) for part in video_size.lower().split("x", 1))

        intro_data = payload.get("intro", {})
        if not isinstance(intro_data, Mapping):
            intro_data = {}
        project_name = str(intro_data.get("title", "")).strip()
        subtitle_name = str(intro_data.get("subtitle", "")).strip()
        if not project_name or not subtitle_name:
            state_path = subtitles_path.with_name("state.json")
            if state_path.exists():
                try:
                    state = _load_json_file(state_path)
                except Exception:
                    state = {}
                if not project_name:
                    project_name = str(state.get("project_name", "")).strip()
                if not subtitle_name:
                    source_name = str(state.get("source_name", "")).strip() or str(state.get("original_audio_name", "")).strip()
                    if source_name:
                        subtitle_name = Path(source_name).stem.strip()
        if not project_name:
            project_name = ""
        if not project_name and audio_path.stem.strip().lower() not in {"no_vocals", "audio"}:
            project_name = audio_path.stem.strip()
        if subtitle_name and subtitle_name == project_name:
            subtitle_name = ""
        if project_name.lower() == "no_vocals":
            project_name = ""
        if not project_name and subtitle_name:
            project_name = subtitle_name
            subtitle_name = ""

        intro_duration = float(intro_data.get("intro_duration_seconds", 2.5) or 2.5)
        intro_card_path = subtitles_path.with_name(str(subtitle_settings.get("assets_dir_name", "subtitle_assets"))) / "intro_title.png"
        if project_name:
            _render_intro_card(
                intro_card_path,
                project_name,
                subtitle_name,
                settings,
                (video_width, video_height),
            )
            intro_event = {
                "start": 0.0,
                "end": intro_duration,
                "image": str(intro_card_path),
                "kind": "intro",
                "fade_in_seconds": 0.25,
                "fade_out_seconds": 0.35,
            }
            events = [intro_event, *events]

        script_path = subtitles_path.with_name("subtitle_overlay.ffscript")
        fade_seconds = max(float(subtitle_settings.get("sentence_preroll_seconds", 1.0)), 0.0)
        image_inputs, video_label = _build_image_overlay_filter_script(events, script_path, y_expression, fade_seconds)
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
