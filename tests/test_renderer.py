from __future__ import annotations

import json
import math
import wave
from pathlib import Path

from modules.renderer import build_render_command, render_video


def _config() -> dict[str, object]:
    return {
        "paths": {"output_dir": "output"},
        "renderer": {
            "ffmpeg_path": "ffmpeg",
            "output_video_name": "karaoke.mp4",
            "video_size": "320x240",
            "frame_rate": 30,
            "background_color": "black",
            "video_codec": "libx264",
            "video_preset": "medium",
            "video_crf": 18,
            "audio_codec": "aac",
            "audio_bitrate": "128k",
            "faststart": True,
        },
        "subtitle_builder": {
            "manifest_name": "subtitles_manifest.json",
            "margin_v": 28,
        },
    }


def _write_test_wav(path: Path, duration_seconds: float = 0.5, sample_rate: int = 44100) -> None:
    num_frames = int(duration_seconds * sample_rate)
    amplitude = 12000
    frequency = 440.0
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        frames = bytearray()
        for index in range(num_frames):
            sample = int(amplitude * math.sin(2 * math.pi * frequency * index / sample_rate))
            frames.extend(sample.to_bytes(2, byteorder="little", signed=True))
        handle.writeframes(bytes(frames))


def test_build_render_command_uses_config(tmp_path: Path) -> None:
    subtitles = tmp_path / "subtitles.ass"
    subtitles.write_text("[Events]\n", encoding="utf-8")

    command = build_render_command(
        Path("temp/no_vocals.wav"),
        subtitles,
        Path("output/karaoke.mp4"),
        _config(),
    )

    assert command[0] == "ffmpeg"
    assert command[command.index("-vf") + 1].startswith("ass='")
    assert command[command.index("-map") + 1] == "0:v:0"
    assert command[-1].endswith("karaoke.mp4")


def test_render_video_creates_mp4(tmp_path: Path) -> None:
    audio_path = tmp_path / "no_vocals.wav"
    subtitles_path = tmp_path / "subtitles.ass"
    output_dir = tmp_path / "output"

    _write_test_wav(audio_path)
    subtitles_path.write_text(
        """[Script Info]
ScriptType: v4.00+
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial Unicode MS,28,&H00FFFFFF,&H0000FFFF,&H00000000,&H7F000000,0,0,0,0,100,100,0,0,1,2,0,2,40,40,28,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:00.40,Default,,0,0,0,,\u200fשלום
""",
        encoding="utf-8",
    )

    config = {
        "paths": {"output_dir": str(output_dir)},
        "renderer": _config()["renderer"],
        "subtitle_builder": _config()["subtitle_builder"],
    }

    output_video = render_video(audio_path, subtitles_path, config)

    assert output_video == output_dir / "karaoke.mp4"
    assert output_video.exists()
    assert output_video.stat().st_size > 0


def test_render_video_prefers_manifest_images_when_present(tmp_path: Path) -> None:
    audio_path = tmp_path / "audio.wav"
    subtitles_path = tmp_path / "subtitles.ass"
    manifest_path = tmp_path / "subtitles_manifest.json"
    image_path = tmp_path / "subtitle_assets" / "line.png"
    output_dir = tmp_path / "output"

    _write_test_wav(audio_path)
    subtitles_path.write_text("[Events]\n", encoding="utf-8")
    image_path.parent.mkdir(parents=True, exist_ok=True)

    # A simple opaque subtitle bar is enough to verify the image-overlay path.
    import PIL.Image

    PIL.Image.new("RGBA", (180, 40), (255, 255, 255, 255)).save(image_path)
    manifest_path.write_text(
        json.dumps({"events": [{"start": 0.0, "end": 0.4, "image": str(image_path)}]}, ensure_ascii=False),
        encoding="utf-8",
    )

    config = {
        "paths": {"output_dir": str(output_dir)},
        "renderer": _config()["renderer"],
        "subtitle_builder": _config()["subtitle_builder"],
    }

    output_video = render_video(audio_path, subtitles_path, config)

    assert output_video.exists()
    assert (tmp_path / "subtitle_overlay.ffscript").exists()


def test_render_video_uses_event_specific_fade_settings(tmp_path: Path) -> None:
    audio_path = tmp_path / "audio.wav"
    subtitles_path = tmp_path / "subtitles.ass"
    manifest_path = tmp_path / "subtitles_manifest.json"
    image_path = tmp_path / "subtitle_assets" / "line.png"
    output_dir = tmp_path / "output"

    _write_test_wav(audio_path)
    subtitles_path.write_text("[Events]\n", encoding="utf-8")
    image_path.parent.mkdir(parents=True, exist_ok=True)

    import PIL.Image

    PIL.Image.new("RGBA", (180, 40), (255, 255, 255, 255)).save(image_path)
    manifest_path.write_text(
        json.dumps(
            {
                "events": [
                    {"start": 0.0, "end": 0.4, "image": str(image_path), "fade_in_seconds": 0.0, "kind": "lyrics"},
                    {
                        "start": 0.4,
                        "end": 0.8,
                        "image": str(image_path),
                        "fade_in_seconds": 0.25,
                        "fade_out_seconds": 0.15,
                        "kind": "intro",
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    config = {
        "paths": {"output_dir": str(output_dir)},
        "renderer": _config()["renderer"],
        "subtitle_builder": _config()["subtitle_builder"],
    }

    output_video = render_video(audio_path, subtitles_path, config)

    assert output_video.exists()
    script_text = (tmp_path / "subtitle_overlay.ffscript").read_text(encoding="utf-8")
    assert script_text.count("fade=t=in") == 1
    assert script_text.count("fade=t=out") == 1
