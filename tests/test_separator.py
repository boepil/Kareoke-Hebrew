from __future__ import annotations

from pathlib import Path

from modules.separator import (
    build_demucs_command,
    expected_demucs_song_dir,
    finalize_separation_outputs,
)


def _config() -> dict[str, object]:
    return {
        "paths": {"temp_dir": "temp"},
        "separator": {
            "demucs_module": "demucs.separate",
            "model_name": "htdemucs",
            "two_stems": "vocals",
            "device": "cpu",
            "shifts": 0,
        },
    }


def test_build_demucs_command_uses_config() -> None:
    command = build_demucs_command(Path("temp/audio.wav"), Path("temp"), _config())

    assert command[1] == "-m"
    assert command[2] == "demucs.separate"
    assert command[command.index("-n") + 1] == "htdemucs"
    assert command[command.index("--two-stems") + 1] == "vocals"
    assert command[command.index("-o") + 1] == "temp"
    assert Path(command[-1]) == Path("temp/audio.wav")


def test_finalize_separation_outputs_copies_expected_files(tmp_path: Path) -> None:
    song_dir = tmp_path / "htdemucs" / "audio"
    song_dir.mkdir(parents=True)
    (song_dir / "vocals.wav").write_bytes(b"vocals")
    (song_dir / "no_vocals.wav").write_bytes(b"backing")

    outputs = finalize_separation_outputs(song_dir, tmp_path)

    assert outputs["vocals"].read_bytes() == b"vocals"
    assert outputs["no_vocals"].read_bytes() == b"backing"
    assert outputs["vocals"] == tmp_path / "vocals.wav"
    assert outputs["no_vocals"] == tmp_path / "no_vocals.wav"


def test_expected_demucs_song_dir_matches_track_stem() -> None:
    song_dir = expected_demucs_song_dir(
        Path("temp/audio.wav"),
        Path("temp"),
        _config(),
    )

    assert song_dir == Path("temp") / "htdemucs" / "audio"
