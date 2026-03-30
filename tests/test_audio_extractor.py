from __future__ import annotations

from pathlib import Path

from modules.audio_extractor import build_ffmpeg_command, extract_and_normalize_audio


def test_build_ffmpeg_command_uses_config() -> None:
    command = build_ffmpeg_command(
        Path("input/test.mp3"),
        Path("temp/audio.wav"),
        {
            "audio_extractor": {
                "ffmpeg_path": "ffmpeg",
                "sample_rate_hz": 44100,
                "channels": 1,
                "audio_codec": "pcm_s16le",
                "normalization_filter": "loudnorm=I=-16:LRA=11:TP=-1.5",
            }
        },
    )

    assert command[0] == "ffmpeg"
    assert command[-1] == "temp/audio.wav"
    assert command[command.index("-ar") + 1] == "44100"
    assert command[command.index("-ac") + 1] == "1"


def test_extract_and_normalize_audio_requires_existing_input(tmp_path: Path) -> None:
    output_file = tmp_path / "audio.wav"

    try:
        extract_and_normalize_audio(
            tmp_path / "missing.mp3",
            output_file,
            {
                "audio_extractor": {
                    "ffmpeg_path": "ffmpeg",
                    "sample_rate_hz": 44100,
                    "channels": 1,
                    "audio_codec": "pcm_s16le",
                    "normalization_filter": "loudnorm=I=-16:LRA=11:TP=-1.5",
                }
            },
        )
    except FileNotFoundError as exc:
        assert "missing.mp3" in str(exc)
    else:
        raise AssertionError("Expected a FileNotFoundError for missing input")
