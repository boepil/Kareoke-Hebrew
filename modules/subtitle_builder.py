"""Build ASS subtitles from segment-level transcript timestamps.

This MVP version writes one subtitle line per Whisper segment. It keeps Hebrew
RTL rendering stable by prefixing each dialogue line with a Unicode RLM marker
and by defining a Hebrew-capable ASS style in the file header.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

import yaml
from PIL import Image, ImageDraw, ImageFont

LOGGER = logging.getLogger(__name__)
RLM = "\u200f"
PLAY_RES_X = 1920
PLAY_RES_Y = 1080

ASS_HEADER_TEMPLATE = """[Script Info]
ScriptType: v4.00+
WrapStyle: 2
ScaledBorderAndShadow: yes
Collisions: Normal
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_name},{font_size},{primary_color},{secondary_color},{outline_color},{back_color},0,0,0,0,100,100,0,0,1,{outline},{shadow},{alignment},{margin_l},{margin_r},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


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


def _subtitle_section(config: Mapping[str, Any]) -> Mapping[str, Any]:
    settings = config.get("subtitle_builder")
    if not isinstance(settings, Mapping):
        raise KeyError("Missing 'subtitle_builder' section in config")
    return settings


def _read_transcript_source(transcript_source: str | Path | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(transcript_source, Mapping):
        return dict(transcript_source)

    transcript_path = Path(transcript_source)
    with transcript_path.open("r", encoding="utf-8") as handle:
        loaded = json.load(handle)
    if not isinstance(loaded, dict):
        raise ValueError(f"Transcript file must contain an object: {transcript_path}")
    return loaded


def build_subtitle_settings(config: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize subtitle builder settings."""
    settings = _subtitle_section(config)
    lyrics_settings = config.get("lyrics_source")
    lyrics_text_name = "lyrics.txt"
    if isinstance(lyrics_settings, Mapping):
        lyrics_text_name = str(lyrics_settings.get("output_text_name", lyrics_text_name))
    return {
        "output_ass_name": str(settings.get("output_ass_name", "subtitles.ass")),
        "font_name": str(settings.get("font_name", "Arial Unicode MS")),
        "font_path": str(settings.get("font_path", "")),
        "font_size": int(settings.get("font_size", 28)),
        "primary_color": str(settings.get("primary_color", "&H00FFFFFF")),
        "secondary_color": str(settings.get("secondary_color", "&H0000FFFF")),
        "outline_color": str(settings.get("outline_color", "&H00000000")),
        "back_color": str(settings.get("back_color", "&H7F000000")),
        "alignment": int(settings.get("alignment", 2)),
        "margin_l": int(settings.get("margin_l", 40)),
        "margin_r": int(settings.get("margin_r", 40)),
        "margin_v": int(settings.get("margin_v", 28)),
        "outline": int(settings.get("outline", 2)),
        "shadow": int(settings.get("shadow", 0)),
        "lyrics_text_name": lyrics_text_name,
        "manifest_name": str(settings.get("manifest_name", "subtitles_manifest.json")),
        "assets_dir_name": str(settings.get("assets_dir_name", "subtitle_assets")),
        "timing_overrides_name": str(settings.get("timing_overrides_name", "timing_overrides.json")),
        "filter_isolated_anchor_segments": bool(settings.get("filter_isolated_anchor_segments", True)),
        "isolated_anchor_gap_seconds": float(settings.get("isolated_anchor_gap_seconds", 10.0)),
        "isolated_anchor_duration_seconds": float(settings.get("isolated_anchor_duration_seconds", 2.0)),
    }


def format_ass_timestamp(seconds: float) -> str:
    """Format a floating-point timestamp for ASS."""
    if seconds < 0:
        raise ValueError("Timestamp cannot be negative")

    total_centiseconds = int(round(seconds * 100))
    hours, remainder = divmod(total_centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    whole_seconds, centiseconds = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{centiseconds:02d}"


def _escape_ass_text(text: str) -> str:
    return text.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")


def _normalize_segment_text(text: str) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    return f"{RLM}{compact}" if compact else RLM


def _split_lyric_words(text: str) -> list[str]:
    return [token for token in re.split(r"\s+", text.strip()) if token]


def _find_font_path(font_name: str, font_path: str) -> str | None:
    if font_path:
        candidate = Path(font_path)
        if candidate.exists():
            return str(candidate)

    direct_candidate = Path(font_name)
    if direct_candidate.exists():
        return str(direct_candidate)

    windows_fonts = Path("C:/Windows/Fonts")
    candidate_files = {
        font_name,
        f"{font_name}.ttf",
        f"{font_name}.ttc",
        "arialuni.ttf",
        "arial.ttf",
        "David.ttf",
        "DavidLibre-Regular.ttf",
    }
    for candidate_name in candidate_files:
        candidate = windows_fonts / candidate_name
        if candidate.exists():
            return str(candidate)
    return None


@lru_cache(maxsize=8)
def _load_measurement_font(font_name: str, font_path: str, font_size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    resolved_path = _find_font_path(font_name, font_path)
    if resolved_path:
        return ImageFont.truetype(resolved_path, font_size)
    try:
        return ImageFont.truetype(font_name, font_size)
    except OSError:
        return ImageFont.load_default()


def _measure_text_width(text: str, settings: Mapping[str, Any]) -> float:
    if not text:
        return 0.0
    font = _load_measurement_font(
        str(settings["font_name"]),
        str(settings.get("font_path", "")),
        int(settings["font_size"]),
    )
    if hasattr(font, "getlength"):
        return float(font.getlength(text))
    bbox = font.getbbox(text)
    return float(bbox[2] - bbox[0])


def _segment_word_windows(segment: Mapping[str, Any]) -> list[tuple[float, float, str]]:
    words = segment.get("words")
    if not isinstance(words, list):
        return []

    windows: list[tuple[float, float, str]] = []
    for word in words:
        if not isinstance(word, Mapping):
            return []
        token = str(word.get("word", "")).strip()
        start = word.get("start")
        end = word.get("end")
        if not token or start is None or end is None:
            return []
        windows.append((float(start), float(end), token))
    return windows


def _uniform_word_windows(text: str, start: float, end: float) -> list[tuple[float, float, str]]:
    words = _split_lyric_words(text)
    if not words:
        return []

    total_duration = max(end - start, 0.01)
    slot_duration = total_duration / len(words)
    windows: list[tuple[float, float, str]] = []
    for index, word in enumerate(words):
        word_start = start + index * slot_duration
        word_end = start + (index + 1) * slot_duration
        windows.append((word_start, word_end, word))
    return windows


def _normalize_display_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _ass_placeholder_line(start: float, end: float, text: str) -> str:
    return (
        f"Dialogue: 0,{format_ass_timestamp(start)},{format_ass_timestamp(end)},Default,,0,0,0,,"
        f"{_escape_ass_text(f'{RLM}{_normalize_display_text(text)}')}"
    )


def _color_to_rgba(ass_color: str) -> tuple[int, int, int, int]:
    cleaned = ass_color.strip().upper().replace("&H", "").replace("&", "")
    cleaned = cleaned.rjust(8, "0")
    alpha = 255 - int(cleaned[0:2], 16)
    blue = int(cleaned[2:4], 16)
    green = int(cleaned[4:6], 16)
    red = int(cleaned[6:8], 16)
    return (red, green, blue, alpha)


def _stroke_fill(settings: Mapping[str, Any]) -> tuple[int, int, int, int]:
    return _color_to_rgba(str(settings["outline_color"]))


def _word_visual_span(
    words: list[str],
    index: int,
    settings: Mapping[str, Any],
) -> tuple[float, float]:
    right_edge = sum(_measure_text_width(word, settings) + (_measure_text_width(" ", settings) if idx < len(words) - 1 else 0.0)
                     for idx, word in enumerate(words[: index + 1]))
    width = _measure_text_width(words[index], settings)
    left_edge = right_edge - width
    return left_edge, right_edge


def _render_line_image(
    display_text: str,
    highlighted_word_count: int,
    output_path: Path,
    settings: Mapping[str, Any],
) -> None:
    normalized_text = _normalize_display_text(display_text)
    words = _split_lyric_words(normalized_text)
    if not words:
        raise ValueError("Cannot render an empty subtitle line")
    display_words = [word[::-1] for word in words]

    font = _load_measurement_font(
        str(settings["font_name"]),
        str(settings.get("font_path", "")),
        int(settings["font_size"]),
    )
    space_width = _measure_text_width(" ", settings)
    outline = int(settings["outline"])
    margin = max(outline + 8, 12)

    word_widths = [_measure_text_width(word, settings) for word in display_words]
    total_width = sum(word_widths) + space_width * max(len(words) - 1, 0)
    bbox = font.getbbox(" ".join(display_words))
    text_height = bbox[3] - bbox[1]

    image_width = int(total_width + margin * 2)
    image_height = int(text_height + margin * 2)
    image = Image.new("RGBA", (image_width, image_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    x = image_width - margin
    y = margin - bbox[1]
    primary_fill = _color_to_rgba(str(settings["primary_color"]))
    secondary_fill = _color_to_rgba(str(settings["secondary_color"]))
    stroke_fill = _stroke_fill(settings)

    for index, word in enumerate(display_words):
        word_width = word_widths[index]
        x -= word_width
        fill = secondary_fill if index < highlighted_word_count else primary_fill
        draw.text(
            (x, y),
            word,
            fill=fill,
            font=font,
            stroke_width=outline,
            stroke_fill=stroke_fill,
        )
        if index < len(words) - 1:
            x -= space_width

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path)


def _build_image_events(
    line_id: str,
    display_text: str,
    start: float,
    end: float,
    word_windows: list[tuple[float, float, str]],
    assets_dir: Path,
    settings: Mapping[str, Any],
) -> list[dict[str, Any]]:
    normalized_text = _normalize_display_text(display_text)
    if not normalized_text:
        return []

    events: list[dict[str, Any]] = []
    if not word_windows:
        image_path = assets_dir / f"{line_id}_base.png"
        _render_line_image(normalized_text, 0, image_path, settings)
        events.append({"start": start, "end": end, "image": str(image_path)})
        return events

    for index, (word_start, word_end, _) in enumerate(word_windows, start=1):
        image_path = assets_dir / f"{line_id}_{index:02d}.png"
        _render_line_image(normalized_text, index, image_path, settings)
        events.append({"start": word_start, "end": word_end, "image": str(image_path)})
    return events


def _read_lyrics_lines(temp_dir: Path, lyrics_text_name: str) -> list[str]:
    lyrics_path = temp_dir / lyrics_text_name
    if not lyrics_path.exists():
        return []

    raw_text = lyrics_path.read_text(encoding="utf-8")
    lines = [re.sub(r"\s+", " ", line).strip() for line in raw_text.splitlines()]
    cleaned = [line for line in lines if line]
    return cleaned


def _read_timing_overrides(temp_dir: Path, timing_overrides_name: str) -> dict[str, Any]:
    overrides_path = temp_dir / timing_overrides_name
    if not overrides_path.exists():
        return {}

    with overrides_path.open("r", encoding="utf-8") as handle:
        loaded = json.load(handle)
    if not isinstance(loaded, dict):
        raise ValueError(f"Timing overrides file must contain an object: {overrides_path}")
    return loaded


def _filter_anchor_segments(
    segments: list[Mapping[str, Any]],
    settings: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    if not settings.get("filter_isolated_anchor_segments", True):
        return segments
    if len(segments) < 2:
        return segments

    filtered: list[Mapping[str, Any]] = []
    gap_threshold = float(settings["isolated_anchor_gap_seconds"])
    duration_threshold = float(settings["isolated_anchor_duration_seconds"])

    for index, segment in enumerate(segments):
        start = float(segment["start"])
        end = float(segment["end"])
        duration = end - start
        prev_end = float(segments[index - 1]["end"]) if index > 0 else None
        next_start = float(segments[index + 1]["start"]) if index < len(segments) - 1 else None
        prev_gap = start - prev_end if prev_end is not None else float("inf")
        next_gap = next_start - end if next_start is not None else float("inf")

        is_isolated = duration <= duration_threshold and (
            prev_gap >= gap_threshold or next_gap >= gap_threshold
        )
        if not is_isolated:
            filtered.append(segment)

    return filtered or segments


def _shift_word_windows(
    word_windows: list[tuple[float, float, str]],
    new_start: float,
    new_end: float,
) -> list[tuple[float, float, str]]:
    if not word_windows:
        return []

    original_start = word_windows[0][0]
    original_end = word_windows[-1][1]
    original_duration = max(original_end - original_start, 0.001)
    new_duration = max(new_end - new_start, 0.001)
    scale = new_duration / original_duration

    shifted: list[tuple[float, float, str]] = []
    for word_start, word_end, word_text in word_windows:
        relative_start = word_start - original_start
        relative_end = word_end - original_start
        shifted.append(
            (
                new_start + relative_start * scale,
                new_start + relative_end * scale,
                word_text,
            )
        )
    return shifted


def _apply_timing_override(
    line_id: str,
    start: float,
    end: float,
    word_windows: list[tuple[float, float, str]],
    overrides: Mapping[str, Any],
) -> tuple[float, float, list[tuple[float, float, str]]]:
    line_overrides = overrides.get("lines", {})
    if not isinstance(line_overrides, Mapping):
        line_overrides = {}

    global_offset = float(overrides.get("global_offset", 0.0))
    current_start = start + global_offset
    current_end = end + global_offset
    current_windows = [(ws + global_offset, we + global_offset, wt) for ws, we, wt in word_windows]

    raw_override = line_overrides.get(line_id)
    if not isinstance(raw_override, Mapping):
        return current_start, current_end, current_windows

    if "offset" in raw_override:
        offset = float(raw_override["offset"])
        current_start += offset
        current_end += offset
        current_windows = [(ws + offset, we + offset, wt) for ws, we, wt in current_windows]

    override_start = raw_override.get("start")
    override_end = raw_override.get("end")
    if override_start is not None or override_end is not None:
        target_start = float(override_start) if override_start is not None else current_start
        target_end = float(override_end) if override_end is not None else current_end
        if target_end <= target_start:
            target_end = target_start + 0.01
        current_windows = _shift_word_windows(current_windows, target_start, target_end)
        current_start = target_start
        current_end = target_end

    if "stretch" in raw_override:
        stretch = max(float(raw_override["stretch"]), 0.01)
        target_end = current_start + (current_end - current_start) * stretch
        current_windows = _shift_word_windows(current_windows, current_start, target_end)
        current_end = target_end

    return current_start, current_end, current_windows


def _timed_lyrics_lines(
    lyrics_lines: list[str],
    segments: list[Mapping[str, Any]],
) -> list[tuple[float, float, str]]:
    if not lyrics_lines:
        return []

    if segments:
        song_start = float(segments[0]["start"])
        song_end = float(segments[-1]["end"])
    else:
        song_start = 0.0
        song_end = float(len(lyrics_lines) * 3)

    total_duration = max(song_end - song_start, float(len(lyrics_lines)))
    line_duration = total_duration / len(lyrics_lines)

    timed_lines: list[tuple[float, float, str]] = []
    for index, lyric_line in enumerate(lyrics_lines):
        start = song_start + index * line_duration
        end = song_start + (index + 1) * line_duration
        timed_lines.append((start, end, lyric_line))
    return timed_lines


def _allocate_line_indices_to_segments(
    lyrics_lines: list[str],
    segments: list[Mapping[str, Any]],
) -> list[list[str]]:
    if not segments:
        return [lyrics_lines]

    durations = [max(float(segment["end"]) - float(segment["start"]), 0.01) for segment in segments]
    total_duration = sum(durations)
    if total_duration <= 0:
        return [lyrics_lines]

    expected_counts = [(duration / total_duration) * len(lyrics_lines) for duration in durations]
    counts = [0 for _ in segments]

    if len(lyrics_lines) >= len(segments):
        counts = [1 for _ in segments]
        remaining = len(lyrics_lines) - len(segments)
        if remaining > 0:
            extras = [max(expected - 1.0, 0.0) for expected in expected_counts]
            extra_total = sum(extras)
            if extra_total <= 0:
                extras = [1.0 for _ in segments]
                extra_total = float(len(segments))

            provisional = [(extra / extra_total) * remaining for extra in extras]
            assigned = [int(value) for value in provisional]
            for index, value in enumerate(assigned):
                counts[index] += value

            leftover = remaining - sum(assigned)
            remainders = sorted(
                ((provisional[index] - assigned[index], index) for index in range(len(segments))),
                reverse=True,
            )
            for _, index in remainders[:leftover]:
                counts[index] += 1
    else:
        chosen_indices = {
            round(position * (len(segments) - 1) / max(len(lyrics_lines) - 1, 1))
            for position in range(len(lyrics_lines))
        }
        while len(chosen_indices) < len(lyrics_lines):
            chosen_indices.add(len(chosen_indices))
        for index in sorted(chosen_indices)[: len(lyrics_lines)]:
            counts[index] = 1

    buckets: list[list[str]] = [[] for _ in segments]
    line_cursor = 0
    for segment_index, count in enumerate(counts):
        for _ in range(count):
            if line_cursor >= len(lyrics_lines):
                break
            buckets[segment_index].append(lyrics_lines[line_cursor])
            line_cursor += 1

    while line_cursor < len(lyrics_lines):
        buckets[-1].append(lyrics_lines[line_cursor])
        line_cursor += 1

    return buckets


def _build_lyrics_dialogue_lines(
    lyrics_lines: list[str],
    segments: list[Mapping[str, Any]],
    settings: Mapping[str, Any],
) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    dialogue_lines: list[str] = []
    image_events: list[dict[str, Any]] = []
    line_entries: list[dict[str, Any]] = []
    assets_dir = Path(settings["assets_dir"])
    overrides = settings.get("timing_overrides", {})
    if not segments:
        for index, (start, end, lyric_line) in enumerate(_timed_lyrics_lines(lyrics_lines, segments)):
            line_id = f"line_{index:03d}"
            word_windows = _uniform_word_windows(lyric_line, start, end)
            start, end, word_windows = _apply_timing_override(line_id, start, end, word_windows, overrides)
            dialogue_lines.append(_ass_placeholder_line(start, end, lyric_line))
            image_events.extend(
                _build_image_events(
                    line_id,
                    lyric_line,
                    start,
                    end,
                    word_windows,
                    assets_dir,
                    settings,
                )
            )
            line_entries.append({"id": line_id, "text": lyric_line, "start": start, "end": end})
        return dialogue_lines, image_events, line_entries

    anchor_segments = _filter_anchor_segments(segments, settings)
    line_buckets = _allocate_line_indices_to_segments(lyrics_lines, anchor_segments)
    line_counter = 0
    for segment, bucket in zip(anchor_segments, line_buckets):
        if not bucket:
            continue

        segment_start = float(segment["start"])
        segment_end = float(segment["end"])
        segment_duration = max(segment_end - segment_start, 0.01)
        slot_duration = segment_duration / len(bucket)

        for index, lyric_line in enumerate(bucket):
            line_start = segment_start + index * slot_duration
            line_end = segment_start + (index + 1) * slot_duration
            line_id = f"line_{line_counter:03d}"
            word_windows = _uniform_word_windows(lyric_line, line_start, line_end)
            line_start, line_end, word_windows = _apply_timing_override(
                line_id,
                line_start,
                line_end,
                word_windows,
                overrides,
            )
            dialogue_lines.append(_ass_placeholder_line(line_start, line_end, lyric_line))
            image_events.extend(
                _build_image_events(
                    line_id,
                    lyric_line,
                    line_start,
                    line_end,
                    word_windows,
                    assets_dir,
                    settings,
                )
            )
            line_entries.append({"id": line_id, "text": lyric_line, "start": line_start, "end": line_end})
            line_counter += 1
    return dialogue_lines, image_events, line_entries


def build_ass_header(settings: Mapping[str, Any]) -> str:
    """Render the ASS file header with the configured styling."""
    return ASS_HEADER_TEMPLATE.format(
        font_name=settings["font_name"],
        font_size=settings["font_size"],
        primary_color=settings["primary_color"],
        secondary_color=settings["secondary_color"],
        outline_color=settings["outline_color"],
        back_color=settings["back_color"],
        outline=settings["outline"],
        shadow=settings["shadow"],
        alignment=settings["alignment"],
        margin_l=settings["margin_l"],
        margin_r=settings["margin_r"],
        margin_v=settings["margin_v"],
    )


def build_ass_dialogue_lines(
    segments: list[Mapping[str, Any]],
    settings: Mapping[str, Any],
) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    """Create ASS dialogue lines from Whisper timestamped segments."""
    dialogue_lines: list[str] = []
    image_events: list[dict[str, Any]] = []
    line_entries: list[dict[str, Any]] = []
    assets_dir = Path(settings["assets_dir"])
    overrides = settings.get("timing_overrides", {})
    for segment in segments:
        segment_start = float(segment["start"])
        segment_end = float(segment["end"])
        word_windows = _segment_word_windows(segment)

        if word_windows:
            display_text = " ".join(word for _, _, word in word_windows)
        else:
            display_text = str(segment.get("text", ""))

        line_id = f"segment_{len(line_entries):03d}"
        segment_start, segment_end, word_windows = _apply_timing_override(
            line_id,
            segment_start,
            segment_end,
            word_windows,
            overrides,
        )
        dialogue_lines.append(_ass_placeholder_line(segment_start, segment_end, display_text))
        image_events.extend(
            _build_image_events(
                line_id,
                display_text,
                segment_start,
                segment_end,
                word_windows,
                assets_dir,
                settings,
            )
        )
        line_entries.append({"id": line_id, "text": display_text, "start": segment_start, "end": segment_end})
    return dialogue_lines, image_events, line_entries


def build_lyrics_ass_dialogue_lines(
    lyrics_lines: list[str],
    segments: list[Mapping[str, Any]],
    settings: Mapping[str, Any],
) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    """Create ASS dialogue lines from imported lyrics text."""
    return _build_lyrics_dialogue_lines(lyrics_lines, segments, settings)


def build_subtitles(
    transcript_source: str | Path | Mapping[str, Any],
    config: str | Path | Mapping[str, Any],
) -> Path:
    """Build an ASS subtitle file from a transcript JSON object or file."""
    config_data = load_config(config)
    transcript = _read_transcript_source(transcript_source)

    paths = _paths_section(config_data)
    settings = build_subtitle_settings(config_data)
    temp_dir = Path(paths["temp_dir"])
    temp_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = temp_dir / settings["assets_dir_name"]
    settings["assets_dir"] = str(assets_dir)
    settings["timing_overrides"] = _read_timing_overrides(temp_dir, settings["timing_overrides_name"])

    segments = transcript.get("segments", [])
    if not isinstance(segments, list):
        raise ValueError("Transcript segments must be a list")

    ass_path = temp_dir / settings["output_ass_name"]
    manifest_path = temp_dir / settings["manifest_name"]
    start = time.perf_counter()
    LOGGER.info("subtitle_builder:start input=%s output=%s", transcript_source, ass_path)

    lyrics_lines = _read_lyrics_lines(temp_dir, settings["lyrics_text_name"])
    if lyrics_lines:
        dialogue_lines, image_events, line_entries = build_lyrics_ass_dialogue_lines(lyrics_lines, segments, settings)
    else:
        dialogue_lines, image_events, line_entries = build_ass_dialogue_lines(segments, settings)

    content = build_ass_header(settings) + "\n".join(dialogue_lines) + "\n"
    ass_path.write_text(content, encoding="utf-8")
    manifest_path.write_text(
        json.dumps({"lines": line_entries, "events": image_events}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    duration = time.perf_counter() - start
    LOGGER.info(
        "subtitle_builder:end output=%s duration_seconds=%.2f",
        ass_path,
        duration,
    )
    return ass_path


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build an ASS subtitle file from Whisper segments.",
    )
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
    build_subtitles(args.transcript_source, args.config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
