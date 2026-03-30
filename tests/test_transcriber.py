from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock, patch

from modules.transcriber import build_transcriber_settings, transcribe_audio


def _config(tmp_dir: str = "temp") -> dict[str, object]:
    return {
        "paths": {"temp_dir": tmp_dir},
        "transcriber": {
            "model_name": "large-v3",
            "device": "cpu",
            "language": "he",
            "task": "transcribe",
            "fp16": False,
            "output_json_name": "transcript.json",
            "output_text_name": "transcript.txt",
        },
    }


def test_build_transcriber_settings_uses_config() -> None:
    settings = build_transcriber_settings(_config())

    assert settings["model_name"] == "large-v3"
    assert settings["device"] == "cpu"
    assert settings["language"] == "he"
    assert settings["fp16"] is False
    assert settings["output_json_name"] == "transcript.json"


def test_transcribe_audio_writes_json_and_text(tmp_path: Path) -> None:
    input_file = tmp_path / "no_vocals.wav"
    input_file.write_bytes(b"fake wav")

    model = Mock()
    model.transcribe.return_value = {
        "language": "he",
        "text": "שלום עולם",
        "segments": [
            {"start": 0.0, "end": 1.2, "text": "שלום"},
            {"start": 1.2, "end": 2.0, "text": "עולם"},
        ],
    }
    whisper_module = Mock()
    whisper_module.load_model.return_value = model

    with patch("modules.transcriber._load_whisper_module", return_value=whisper_module):
        result = transcribe_audio(input_file, _config(tmp_dir=str(tmp_path)))

    json_path = tmp_path / "transcript.json"
    text_path = tmp_path / "transcript.txt"

    assert json_path.exists()
    assert text_path.exists()
    assert text_path.read_text(encoding="utf-8") == "שלום עולם"

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["source_file"] == str(input_file)
    assert payload["language"] == "he"
    assert len(payload["segments"]) == 2
    assert result["json_path"] == str(json_path)
    assert result["text_path"] == str(text_path)


def test_transcribe_audio_requires_existing_input(tmp_path: Path) -> None:
    try:
        transcribe_audio(tmp_path / "missing.wav", _config(tmp_dir=str(tmp_path)))
    except FileNotFoundError as exc:
        assert "missing.wav" in str(exc)
    else:
        raise AssertionError("Expected a FileNotFoundError for missing input")
