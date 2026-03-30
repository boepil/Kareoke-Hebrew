from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock, patch

from modules.aligner import align_transcript, build_aligner_settings


def _config(tmp_dir: str = "temp") -> dict[str, object]:
    return {
        "paths": {"temp_dir": tmp_dir},
        "aligner": {
            "device": "cpu",
            "compute_type": "int8",
            "output_json_name": "aligned.json",
        },
    }


def test_build_aligner_settings_uses_config() -> None:
    settings = build_aligner_settings(_config())

    assert settings["device"] == "cpu"
    assert settings["compute_type"] == "int8"
    assert settings["output_json_name"] == "aligned.json"


def test_align_transcript_writes_aligned_json(tmp_path: Path) -> None:
    audio_path = tmp_path / "no_vocals.wav"
    audio_path.write_bytes(b"fake wav")
    transcript_path = tmp_path / "transcript.json"
    transcript_path.write_text(
        json.dumps(
            {
                "language": "he",
                "text": "שלום עולם",
                "segments": [
                    {"start": 0.0, "end": 1.2, "text": "שלום עולם"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    whisperx = Mock()
    whisperx.load_audio.return_value = [0.0, 0.1]
    whisperx.load_align_model.return_value = (Mock(), Mock())
    whisperx.align.return_value = {
        "language": "he",
        "segments": [
            {
                "start": 0.0,
                "end": 1.2,
                "text": "שלום עולם",
                "words": [
                    {"start": 0.0, "end": 0.5, "word": "שלום"},
                    {"start": 0.5, "end": 1.2, "word": "עולם"},
                ],
            }
        ],
    }

    with patch("modules.aligner._load_whisperx_module", return_value=whisperx):
        result = align_transcript(audio_path, transcript_path, _config(tmp_dir=str(tmp_path)))

    output_path = tmp_path / "aligned.json"
    assert output_path.exists()
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["language"] == "he"
    assert payload["segments"][0]["words"][0]["word"] == "שלום"
    assert result["json_path"] == str(output_path)


def test_align_transcript_requires_existing_audio(tmp_path: Path) -> None:
    transcript_path = tmp_path / "transcript.json"
    transcript_path.write_text(
        json.dumps({"language": "he", "segments": []}, ensure_ascii=False),
        encoding="utf-8",
    )

    try:
        align_transcript(tmp_path / "missing.wav", transcript_path, _config(tmp_dir=str(tmp_path)))
    except FileNotFoundError as exc:
        assert "missing.wav" in str(exc)
    else:
        raise AssertionError("Expected a FileNotFoundError for missing audio")
