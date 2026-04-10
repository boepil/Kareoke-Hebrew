from __future__ import annotations

import json
from pathlib import Path

from modules.subtitle_builder import (
    build_ass_dialogue_lines,
    build_ass_header,
    build_subtitle_settings,
    build_subtitles,
    format_ass_timestamp,
)


def _config(tmp_dir: str = "temp") -> dict[str, object]:
    return {
        "paths": {"temp_dir": tmp_dir},
        "subtitle_builder": {
            "output_ass_name": "subtitles.ass",
            "manifest_name": "subtitles_manifest.json",
            "assets_dir_name": "subtitle_assets",
            "timing_overrides_name": "timing_overrides.json",
            "filter_isolated_anchor_segments": True,
            "isolated_anchor_gap_seconds": 10.0,
            "isolated_anchor_duration_seconds": 2.0,
            "font_name": "Arial Unicode MS",
            "font_size": 28,
            "primary_color": "&H00FFFFFF",
            "secondary_color": "&H0000FFFF",
            "outline_color": "&H00000000",
            "back_color": "&H7F000000",
            "alignment": 2,
            "margin_l": 40,
            "margin_r": 40,
            "margin_v": 28,
            "outline": 2,
            "shadow": 0,
        },
    }


def test_format_ass_timestamp() -> None:
    assert format_ass_timestamp(0) == "0:00:00.00"
    assert format_ass_timestamp(61.23) == "0:01:01.23"


def test_build_ass_dialogue_lines_prefixes_rlm() -> None:
    settings = build_subtitle_settings(_config())
    settings["assets_dir"] = "temp/subtitle_assets"
    lines, events, entries = build_ass_dialogue_lines(
        [{"start": 0.0, "end": 1.5, "text": "שלום עולם"}],
        settings,
    )

    assert len(lines) == 1
    assert lines[0].endswith("\u200fשלום עולם")
    assert "\\clip(" not in lines[0]
    assert len(events) == 1
    assert entries[0]["id"] == "segment_000"


def test_build_ass_dialogue_lines_uses_clip_overlays_for_word_timings() -> None:
    settings = build_subtitle_settings(_config())
    settings["assets_dir"] = "temp/subtitle_assets"
    lines, events, entries = build_ass_dialogue_lines(
        [
            {
                "start": 0.0,
                "end": 1.5,
                "text": "שלום עולם",
                "words": [
                    {"start": 0.0, "end": 0.5, "word": "שלום"},
                    {"start": 0.5, "end": 1.5, "word": "עולם"},
                ],
            }
        ],
        settings,
    )

    assert len(lines) == 1
    assert lines[0].startswith("Dialogue: 0,0:00:00.00,0:00:01.50,Default")
    assert "שלום עולם" in lines[0]
    assert len(events) == 2
    assert float(events[0]["end"]) == 1.5
    assert float(events[1]["end"]) == 1.5
    assert entries[0]["start"] == 0.0


def test_build_ass_header_contains_hebrew_friendly_style() -> None:
    header = build_ass_header(build_subtitle_settings(_config()))

    assert "ScaledBorderAndShadow: yes" in header
    assert "Arial Unicode MS" in header
    assert "Style: Default" in header


def test_build_subtitles_writes_ass_file(tmp_path: Path) -> None:
    transcript_path = tmp_path / "transcript.json"
    transcript_path.write_text(
        json.dumps(
            {
                "text": "שלום עולם",
                "segments": [
                    {"start": 0.0, "end": 1.2, "text": "שלום"},
                    {"start": 1.2, "end": 2.3, "text": "עולם"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    ass_path = build_subtitles(transcript_path, _config(tmp_dir=str(tmp_path)))

    assert ass_path == tmp_path / "subtitles.ass"
    content = ass_path.read_text(encoding="utf-8")
    assert "ScaledBorderAndShadow: yes" in content
    assert "Dialogue: 0,0:00:00.00,0:00:01.20,Default" in content
    assert "\u200fשלום" in content
    assert "\u200fעולם" in content
    manifest = json.loads((tmp_path / "subtitles_manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["events"]) == 2
    assert len(manifest["lines"]) == 2


def test_build_subtitles_writes_progressive_overlay_when_words_exist(tmp_path: Path) -> None:
    transcript_path = tmp_path / "aligned.json"
    transcript_path.write_text(
        json.dumps(
            {
                "text": "שלום עולם",
                "segments": [
                    {
                        "start": 0.0,
                        "end": 1.5,
                        "text": "שלום עולם",
                        "words": [
                            {"start": 0.0, "end": 0.5, "word": "שלום"},
                            {"start": 0.5, "end": 1.5, "word": "עולם"},
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    ass_path = build_subtitles(transcript_path, _config(tmp_dir=str(tmp_path)))

    content = ass_path.read_text(encoding="utf-8")
    assert "שלום עולם" in content
    assert "{\\k" not in content
    manifest = json.loads((tmp_path / "subtitles_manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["events"]) == 2
    assert manifest["lines"][0]["id"] == "segment_000"
    assert Path(manifest["events"][0]["image"]).exists()


def test_build_subtitles_prefers_imported_lyrics_text(tmp_path: Path) -> None:
    temp_dir = tmp_path
    (temp_dir / "lyrics.txt").write_text("בית ראשון\nפזמון", encoding="utf-8")
    transcript_path = temp_dir / "aligned.json"
    transcript_path.write_text(
        json.dumps(
            {
                "text": "ignored",
                "segments": [
                    {"start": 0.0, "end": 2.0, "text": "ignored"},
                    {"start": 2.0, "end": 4.0, "text": "ignored"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    ass_path = build_subtitles(transcript_path, _config(tmp_dir=str(temp_dir)))

    content = ass_path.read_text(encoding="utf-8")
    assert "בית ראשון" in content
    assert "פזמון" in content
    assert "ignored" not in content
    manifest = json.loads((temp_dir / "subtitles_manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["events"]) == 4
    assert manifest["lines"][0]["id"] == "line_000"


def test_build_subtitles_anchors_lyrics_to_aligned_segments(tmp_path: Path) -> None:
    temp_dir = tmp_path
    (temp_dir / "lyrics.txt").write_text("שורה א\nשורה ב\nשורה ג", encoding="utf-8")
    transcript_path = temp_dir / "aligned.json"
    transcript_path.write_text(
        json.dumps(
            {
                "segments": [
                    {"start": 5.0, "end": 6.0, "text": "a"},
                    {"start": 20.0, "end": 30.0, "text": "b"},
                    {"start": 40.0, "end": 41.5, "text": "c"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    config = _config(tmp_dir=str(temp_dir))
    config["subtitle_builder"]["filter_isolated_anchor_segments"] = False
    ass_path = build_subtitles(transcript_path, config)

    lines = [line for line in ass_path.read_text(encoding="utf-8").splitlines() if line.startswith("Dialogue: 0,")]
    assert len(lines) == 3
    assert "0:00:05.00" in lines[0]
    assert "0:00:06.00" in lines[0]
    assert "0:00:20.00" in lines[1]
    assert "0:00:30.00" in lines[1]
    assert "0:00:40.00" in lines[2]
    assert "0:00:41.50" in lines[2]
    manifest = json.loads((temp_dir / "subtitles_manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["events"]) >= 9
    assert any(event.get("kind") == "countdown" for event in manifest["events"])
    assert len(manifest["lines"]) == 3


def test_build_subtitles_filters_isolated_intro_anchor_segment(tmp_path: Path) -> None:
    (tmp_path / "lyrics.txt").write_text("שורה ראשונה\nשורה שניה", encoding="utf-8")
    transcript_path = tmp_path / "aligned.json"
    transcript_path.write_text(
        json.dumps(
            {
                "segments": [
                    {"start": 0.1, "end": 1.0, "text": "noise"},
                    {"start": 30.0, "end": 33.0, "text": "verse"},
                    {"start": 33.0, "end": 36.0, "text": "verse2"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    build_subtitles(transcript_path, _config(tmp_dir=str(tmp_path)))
    manifest = json.loads((tmp_path / "subtitles_manifest.json").read_text(encoding="utf-8"))

    assert manifest["lines"][0]["start"] >= 30.0


def test_build_subtitles_applies_timing_override_file(tmp_path: Path) -> None:
    (tmp_path / "lyrics.txt").write_text("שורה ראשונה", encoding="utf-8")
    (tmp_path / "timing_overrides.json").write_text(
        json.dumps(
            {
                "global_offset": 1.0,
                "lines": {
                    "line_000": {"start": 12.5, "end": 14.0}
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    transcript_path = tmp_path / "aligned.json"
    transcript_path.write_text(
        json.dumps(
            {"segments": [{"start": 5.0, "end": 7.0, "text": "x"}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    build_subtitles(transcript_path, _config(tmp_dir=str(tmp_path)))
    manifest = json.loads((tmp_path / "subtitles_manifest.json").read_text(encoding="utf-8"))

    assert manifest["lines"][0]["start"] == 12.5
    assert manifest["lines"][0]["end"] == 14.0
    assert any(event.get("kind") == "lyrics_preroll" and abs(float(event["start"]) - 11.5) < 0.001 for event in manifest["events"])


def test_build_subtitles_adds_countdown_events_for_intro_and_long_gap(tmp_path: Path) -> None:
    (tmp_path / "lyrics.txt").write_text("line one\nline two", encoding="utf-8")
    transcript_path = tmp_path / "aligned.json"
    transcript_path.write_text(
        json.dumps(
            {
                "segments": [
                    {"start": 5.0, "end": 6.0, "text": "first"},
                    {"start": 12.0, "end": 13.0, "text": "second"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    build_subtitles(transcript_path, _config(tmp_dir=str(tmp_path)))
    manifest = json.loads((tmp_path / "subtitles_manifest.json").read_text(encoding="utf-8"))

    countdown_events = [event for event in manifest["events"] if event.get("kind") == "countdown"]
    assert [event.get("text") for event in countdown_events[:3]] == ["3", "2", "1"]
    assert any(event.get("text") == "3" and abs(float(event["start"]) - 9.0) < 0.001 for event in countdown_events)
