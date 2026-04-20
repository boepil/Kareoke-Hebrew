from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import requests

from modules.editor_project import export_editor_project
from modules.first_pass import run_first_pass_autosync
from modules.lyrics_corrector import correct_transcript_with_lm_studio


def _write_wav(path: Path, frames: int = 4410) -> None:
    import wave

    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(44100)
        samples = bytearray()
        for index in range(frames):
            amplitude = 12000 if index % 2 == 0 else -12000
            samples.extend(int(amplitude).to_bytes(2, byteorder="little", signed=True))
        wav_file.writeframes(bytes(samples))


def _config(tmp_path: Path) -> dict[str, object]:
    temp_dir = tmp_path / "data"
    input_dir = tmp_path / "input"
    temp_dir.mkdir(parents=True)
    input_dir.mkdir(parents=True)
    return {
        "paths": {
            "temp_dir": "data",
            "input_dir": "data/input",
        },
        "subtitle_builder": {
            "manifest_name": "subtitles_manifest.json",
            "timing_overrides_name": "timing_overrides.json",
        },
        "autosync": {
            "enabled": True,
            "correction_enabled": True,
            "correction_endpoint_url": "http://127.0.0.1:1234/v1/chat/completions",
            "correction_model_id": "gemma-4-9b-it",
        },
    }


def test_correction_skips_when_lm_studio_unreachable(tmp_path: Path) -> None:
    transcript = {
        "language": "he",
        "text": "rough text",
        "segments": [{"id": 0, "start": 0.0, "end": 1.0, "text": "rough text"}],
    }

    with patch("modules.lyrics_corrector.requests.post", side_effect=requests.ConnectionError("down")):
        corrected = correct_transcript_with_lm_studio(transcript, "הטקסט", _config(tmp_path))

    assert corrected["correction_status"] == "skipped"
    assert corrected["text"] == transcript["text"]
    assert corrected["segments"] == transcript["segments"]


def test_export_editor_project_writes_committed_words(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    temp_dir = tmp_path / "data"
    audio_wav = temp_dir / "audio.wav"
    vocals_wav = temp_dir / "vocals.wav"
    no_vocals_wav = temp_dir / "no_vocals.wav"
    _write_wav(audio_wav)
    _write_wav(vocals_wav)
    _write_wav(no_vocals_wav)

    aligned = {
        "segments": [
            {
                "start": 0.0,
                "end": 2.0,
                "words": [
                    {"word": "אחד", "start": 0.0, "end": 0.8},
                    {"word": "שתיים", "start": 0.8, "end": 1.6},
                ],
            }
        ]
    }

    project = export_editor_project(
        cfg,
        project_name="זמר - שיר",
        source_name="song.mp3",
        original_audio_name="song.mp3",
        lyrics_text="אחד שתיים",
        audio_artifacts={
            "audio_source": tmp_path / "song.mp3",
            "audio_wav": audio_wav,
            "vocals_wav": vocals_wav,
            "no_vocals_wav": no_vocals_wav,
        },
        aligned_transcript=aligned,
        lyrics_source_url="https://shirrim.com/song-lyrics/example/",
    )

    overrides = json.loads(Path(project["overrides_path"]).read_text(encoding="utf-8"))
    manifest = json.loads(Path(project["manifest_path"]).read_text(encoding="utf-8"))
    state = json.loads(Path(project["state_path"]).read_text(encoding="utf-8"))

    assert overrides["placed_word_count"] == 2
    assert len(overrides["words"]) == 2
    assert len(manifest["words"]) == 2
    assert state["project_name"] == "זמר - שיר"
    current_marker = tmp_path / "data" / ".current_project"
    assert current_marker.exists()
    assert current_marker.read_text(encoding="utf-8") == project["project_id"]


def test_first_pass_pipeline_uses_vocals_stem(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    temp_dir = Path(cfg["paths"]["temp_dir"])
    input_file = tmp_path / "song.mp3"
    input_file.write_bytes(b"fake mp3")

    def fake_extract(input_path, output_path, config):
        _write_wav(Path(output_path))
        return Path(output_path)

    def fake_separate(input_path, config):
        vocals = temp_dir / "vocals.wav"
        no_vocals = temp_dir / "no_vocals.wav"
        _write_wav(vocals)
        _write_wav(no_vocals)
        return {"vocals": vocals, "no_vocals": no_vocals}

    def fake_transcribe(input_path, config):
        assert Path(input_path).name == "vocals.wav"
        transcript_path = temp_dir / "transcript.json"
        transcript_text_path = temp_dir / "transcript.txt"
        payload = {
            "source_file": str(input_path),
            "provider": "gemma",
            "language": "he",
            "text": "אחד שתיים",
            "segments": [{"id": 0, "start": 0.0, "end": 2.0, "text": "אחד שתיים"}],
            "json_path": str(transcript_path),
            "text_path": str(transcript_text_path),
        }
        transcript_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        transcript_text_path.write_text("אחד שתיים", encoding="utf-8")
        return payload

    def fake_align(audio_file, transcript_source, config):
        assert Path(audio_file).name == "vocals.wav"
        aligned_path = temp_dir / "aligned.json"
        payload = {
            "segments": [
                {
                    "start": 0.0,
                    "end": 2.0,
                    "words": [
                        {"word": "אחד", "start": 0.0, "end": 0.8},
                        {"word": "שתיים", "start": 0.8, "end": 1.6},
                    ],
                }
            ],
            "json_path": str(aligned_path),
        }
        aligned_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return payload

    with patch("modules.first_pass.extract_and_normalize_audio", side_effect=fake_extract), patch(
        "modules.first_pass.separate_vocals",
        side_effect=fake_separate,
    ), patch(
        "modules.first_pass.transcribe_audio",
        side_effect=fake_transcribe,
    ), patch(
        "modules.first_pass.correct_transcript_with_lm_studio",
        side_effect=lambda transcript, lyrics_text, config: transcript,
    ), patch(
        "modules.first_pass.align_transcript",
        side_effect=fake_align,
    ):
        result = run_first_pass_autosync(
            input_file,
            cfg,
            project_name="זמר - שיר",
            source_name="song.mp3",
            original_audio_name="song.mp3",
            lyrics_text="אחד שתיים",
            lyrics_source_url="https://shirrim.com/song-lyrics/example/",
        )

    assert Path(result["vocals_wav"]).name == "vocals.wav"
    assert Path(result["project"]["state_path"]).exists()
    assert json.loads(Path(result["project"]["overrides_path"]).read_text(encoding="utf-8"))["placed_word_count"] == 2
