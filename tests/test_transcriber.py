from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock, patch

from modules.transcriber import build_transcriber_settings, transcribe_audio


def _config(tmp_dir: str = "temp") -> dict[str, object]:
    return {
        "paths": {"temp_dir": tmp_dir},
        "transcriber": {
            "provider": "whisper",
            "fallback_to_whisper": True,
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

    assert settings["provider"] == "whisper"
    assert settings["fallback_to_whisper"] is True
    assert settings["model_name"] == "large-v3"
    assert settings["device"] == "cpu"
    assert settings["language"] == "he"
    assert settings["fp16"] is False
    assert settings["output_json_name"] == "transcript.json"


def test_build_transcriber_settings_uses_autosync_provider_when_enabled() -> None:
    config = _config()
    del config["transcriber"]["provider"]  # type: ignore[index]
    config["autosync"] = {
        "enabled": True,
        "provider": "gemma",
        "fallback_to_whisper": True,
        "language": "he",
    }

    settings = build_transcriber_settings(config)

    assert settings["provider"] == "gemma"
    assert settings["fallback_to_whisper"] is True


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


def test_transcribe_audio_uses_gemma_provider_when_configured(tmp_path: Path) -> None:
    input_file = tmp_path / "vocals.wav"
    input_file.write_bytes(b"fake wav")

    config = _config(tmp_dir=str(tmp_path))
    config["transcriber"]["provider"] = "gemma"  # type: ignore[index]

    with patch(
        "modules.transcriber._load_gemma_transcriber",
        return_value=Mock(return_value={"provider": "gemma", "json_path": "a", "text_path": "b"}),
    ) as load_gemma, patch(
        "modules.transcriber._load_whisper_module",
        side_effect=AssertionError("whisper should not be loaded"),
    ):
        payload = transcribe_audio(input_file, config)

    assert payload["provider"] == "gemma"
    load_gemma.assert_called_once()


def test_transcribe_audio_falls_back_to_whisper_when_gemma_errors(tmp_path: Path) -> None:
    input_file = tmp_path / "vocals.wav"
    input_file.write_bytes(b"fake wav")

    config = _config(tmp_dir=str(tmp_path))
    config["transcriber"]["provider"] = "gemma"  # type: ignore[index]
    config["transcriber"]["fallback_to_whisper"] = True  # type: ignore[index]

    model = Mock()
    model.transcribe.return_value = {
        "language": "he",
        "text": "fallback transcript",
        "segments": [{"start": 0.0, "end": 1.0, "text": "fallback transcript"}],
    }
    whisper_module = Mock()
    whisper_module.load_model.return_value = model

    with patch(
        "modules.transcriber._load_gemma_transcriber",
        return_value=Mock(side_effect=RuntimeError("gemma failed")),
    ), patch(
        "modules.transcriber._load_whisper_module",
        return_value=whisper_module,
    ):
        payload = transcribe_audio(input_file, config)

    assert payload["provider"] == "whisper"
    assert payload["text"] == "fallback transcript"
    assert (tmp_path / "transcript.json").exists()
    assert (tmp_path / "transcript.txt").read_text(encoding="utf-8") == "fallback transcript"
