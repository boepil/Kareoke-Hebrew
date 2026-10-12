from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from modules.separator import (
    SeparationCancelledError,
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


def test_separate_vocals_uses_in_process_api_and_reports_progress(tmp_path: Path) -> None:
    import modules.separator as separator_module

    class FakeStem:
        def __init__(self, value: int) -> None:
            self.value = value

        def clone(self) -> "FakeStem":
            return FakeStem(self.value)

        def __iadd__(self, other: "FakeStem") -> "FakeStem":
            self.value += other.value
            return self

    class FakeModel:
        segment = 7.8
        sources = ["drums", "bass", "other", "vocals"]

    class FakeSeparator:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            self.model = FakeModel()
            self.samplerate = 44100
            self.callback = None

        def update_parameter(self, *, callback: object) -> None:
            self.callback = callback

        def separate_audio_file(self, input_file: Path) -> tuple[None, dict[str, FakeStem]]:
            assert input_file.exists()
            assert callable(self.callback)
            self.callback({
                "state": "end", "model_idx_in_bag": 0, "shift_idx": 0,
                "segment_offset": 0, "audio_length": 1000,
            })
            return None, {
                "vocals": FakeStem(10),
                "drums": FakeStem(1),
                "bass": FakeStem(2),
                "other": FakeStem(3),
            }

    input_file = tmp_path / "audio.wav"
    input_file.write_bytes(b"test")
    saved: dict[str, int] = {}

    def fake_save_audio(stem: FakeStem, path: Path, **kwargs: object) -> None:
        saved[Path(path).name] = stem.value
        Path(path).write_bytes(str(stem.value).encode())

    progress: list[str] = []
    with patch("demucs.api.Separator", FakeSeparator), patch(
        "demucs.api.save_audio", side_effect=fake_save_audio
    ):
        outputs = separator_module.separate_vocals(
            input_file, {"paths": {"temp_dir": str(tmp_path / "temp")},
                         "separator": {"model_name": "htdemucs", "device": "cpu",
                                      "shifts": 0, "two_stems": "vocals"}},
            progress_callback=progress.append,
        )

    assert outputs["vocals"].read_bytes() == b"10"
    assert outputs["no_vocals"].read_bytes() == b"6"
    assert saved == {"vocals.wav": 10, "no_vocals.wav": 6}
    assert any("Separating stems" in item for item in progress)
    assert progress[-1] == "Vocal and accompaniment stems are ready"


def test_separate_vocals_honors_cancellation_before_loading_model(tmp_path: Path) -> None:
    import pytest
    import modules.separator as separator_module

    input_file = tmp_path / "audio.wav"
    input_file.write_bytes(b"test")
    with pytest.raises(SeparationCancelledError, match="cancelled"):
        separator_module.separate_vocals(
            input_file,
            {"paths": {"temp_dir": str(tmp_path / "temp")},
             "separator": {"model_name": "htdemucs", "device": "cpu",
                           "shifts": 0, "two_stems": "vocals"}},
            cancel_check=lambda: True,
        )
