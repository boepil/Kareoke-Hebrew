from __future__ import annotations

import json
import os
import wave
from pathlib import Path
from unittest.mock import patch

from timing_editor import _build_manual_subtitles, _default_output_video_path, _editor_paths, _project_paths, create_app


def _write_config(path: Path, temp_dir: Path, output_dir: Path, logs_dir: Path) -> None:
    path.write_text(
        f"""paths:\n  input_dir: {temp_dir.parent / 'input'}\n  output_dir: {output_dir}\n  temp_dir: {temp_dir}\n  logs_dir: {logs_dir}\n\nsubtitle_builder:\n  manifest_name: subtitles_manifest.json\n  timing_overrides_name: timing_overrides.json\n""",
        encoding="utf-8",
    )


def _write_wav(path: Path, frames: int = 4410) -> None:
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(44100)
        samples = bytearray()
        for index in range(frames):
            amplitude = 12000 if index % 2 == 0 else -12000
            samples.extend(int(amplitude).to_bytes(2, byteorder="little", signed=True))
        wav_file.writeframes(bytes(samples))


def test_timing_editor_api_round_trip(tmp_path: Path) -> None:
    temp_dir = tmp_path / "temp"
    output_dir = tmp_path / "output"
    logs_dir = tmp_path / "logs"
    input_dir = tmp_path / "input"
    temp_dir.mkdir()
    output_dir.mkdir()
    logs_dir.mkdir()
    input_dir.mkdir()

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, temp_dir, output_dir, logs_dir)

    manifest_path = temp_dir / "subtitles_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "lines": [
                    {"id": "line_000", "text": "hello world", "start": 1.0, "end": 2.0, "word_ids": ["word_0000", "word_0001"]},
                    {"id": "line_001", "text": "more words", "start": 3.0, "end": 4.0, "word_ids": ["word_0002", "word_0003"]},
                ],
                "words": [
                    {"id": "word_0000", "text": "hello", "start": 1.0, "end": 1.4, "line_index": 0},
                    {"id": "word_0001", "text": "world", "start": 1.4, "end": 2.0, "line_index": 0},
                    {"id": "word_0002", "text": "more", "start": 3.0, "end": 3.4, "line_index": 1},
                    {"id": "word_0003", "text": "words", "start": 3.4, "end": 4.0, "line_index": 1},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (temp_dir / "timing_overrides.json").write_text(
        json.dumps({"global_offset": 0.0, "lines": {}, "words": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    _write_wav(temp_dir / "audio.wav")

    app = create_app(config_path)
    app.testing = True
    client = app.test_client()

    session_response = client.get("/api/session")
    assert session_response.status_code == 200
    session_payload = session_response.get_json()
    assert session_payload["output_video_name"] == "karaoke.mp4"

    manifest_response = client.get("/api/manifest")
    assert manifest_response.status_code == 200
    manifest_payload = manifest_response.get_json()
    assert manifest_payload["lines"][0]["text"] == "hello world"
    assert manifest_payload["words"][0]["text"] == "hello"

    waveform_response = client.get("/api/waveform?bins=32")
    assert waveform_response.status_code == 200
    waveform_payload = waveform_response.get_json()
    assert waveform_payload["duration"] > 0
    assert len(waveform_payload["peaks"]) >= 1

    audio_response = client.get("/api/audio")
    assert audio_response.status_code == 200
    assert audio_response.mimetype == "audio/wav"

    save_response = client.post(
        "/api/overrides",
        json={
            "global_offset": 0.15,
            "placed_word_count": 2,
            "lyrics_text": "hello world\nmore words",
            "lines": {"line_000": {"start": 1.25, "end": 2.35}},
            "words": {"word_0001": {"start": 1.45, "end": 2.05}},
        },
    )
    assert save_response.status_code == 200
    saved_payload = save_response.get_json()
    assert saved_payload["global_offset"] == 0.15
    assert saved_payload["placed_word_count"] == 2
    assert saved_payload["lyrics_text"] == "hello world\nmore words"
    assert saved_payload["lines"]["line_000"]["start"] == 1.25
    assert saved_payload["words"]["word_0001"]["start"] == 1.45

    on_disk = json.loads((temp_dir / "timing_overrides.json").read_text(encoding="utf-8"))
    assert on_disk["placed_word_count"] == 2
    assert on_disk["lyrics_text"] == "hello world\nmore words"
    assert on_disk["lines"]["line_000"]["end"] == 2.35
    assert on_disk["words"]["word_0001"]["end"] == 2.05
    assert (temp_dir / "lyrics.txt").read_text(encoding="utf-8") == "hello world\nmore words"


def test_timing_editor_import_creates_manual_project(tmp_path: Path) -> None:
    temp_dir = tmp_path / "temp"
    output_dir = tmp_path / "output"
    logs_dir = tmp_path / "logs"
    input_dir = tmp_path / "input"
    temp_dir.mkdir()
    output_dir.mkdir()
    logs_dir.mkdir()
    input_dir.mkdir()

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, temp_dir, output_dir, logs_dir)

    audio_file = tmp_path / "song.mp3"
    audio_file.write_bytes(b"fake mp3")

    app = create_app(config_path)
    app.testing = True
    client = app.test_client()

    def fake_extract(input_file, output_file, config):
        assert Path(input_file).name == "song.mp3"
        _write_wav(Path(output_file))
        return Path(output_file)

    def fake_separate(input_file, config):
        vocals_path = Path(input_file).with_name("vocals.wav")
        no_vocals_path = Path(input_file).with_name("no_vocals.wav")
        vocals_path.write_bytes(b"vocals")
        no_vocals_path.write_bytes(b"backing")
        return {"vocals": vocals_path, "no_vocals": no_vocals_path}

    with patch("timing_editor.extract_and_normalize_audio", side_effect=fake_extract), patch(
        "timing_editor.separate_vocals",
        side_effect=fake_separate,
    ), audio_file.open("rb") as fh:
        response = client.post(
            "/api/import",
            data={
                "audio_file": (fh, "song.mp3"),
                "lyrics_text": "hello\nworld",
            },
            content_type="multipart/form-data",
        )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["project_id"] == "song"

    paths = _project_paths(_editor_paths(config_path), "song")
    manifest = json.loads(paths.editor_manifest_path.read_text(encoding="utf-8"))
    assert len(manifest["lines"]) == 2
    assert len(manifest["words"]) == 2
    assert manifest["lines"][0]["text"] == "hello"
    assert manifest["words"][0]["text"] == "hello"
    assert manifest["words"][1]["start"] > manifest["words"][0]["start"]

    state = json.loads(paths.state_path.read_text(encoding="utf-8"))
    assert state["mode"] == "manual"
    assert state["line_count"] == 2
    assert state["word_count"] == 2
    assert state["lyrics_text"] == "hello\nworld"
    assert state["source_name"] == "song.mp3"
    assert state["project_name"] == "song"
    assert Path(state["artifacts"]["audio_wav"]).name == "audio.wav"
    assert "no_vocals_wav" not in state["artifacts"]

    assert _default_output_video_path(paths).name == "song (Kareoke).mp4"

    session_response = client.get("/api/session")
    assert session_response.status_code == 200
    session_payload = session_response.get_json()
    assert session_payload["lyrics_text"] == "hello\nworld"
    assert session_payload["source_name"] == "song.mp3"
    assert session_payload["output_video_name"] == "song (Kareoke).mp4"


def test_timing_editor_import_preserves_unicode_filename_for_output(tmp_path: Path) -> None:
    temp_dir = tmp_path / "temp"
    output_dir = tmp_path / "output"
    logs_dir = tmp_path / "logs"
    input_dir = tmp_path / "input"
    temp_dir.mkdir()
    output_dir.mkdir()
    logs_dir.mkdir()
    input_dir.mkdir()

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, temp_dir, output_dir, logs_dir)

    project_name = "\u05d2\u05d6\u05d5\u05d6 - \u05d7\u05dc\u05dc\u05d9\u05ea"
    audio_filename = f"{project_name}.mp3"
    audio_file = tmp_path / "source.mp3"
    audio_file.write_bytes(b"fake mp3")

    app = create_app(config_path)
    app.testing = True
    client = app.test_client()

    def fake_extract(input_file, output_file, config):
        assert Path(input_file).name == audio_filename
        _write_wav(Path(output_file))
        return Path(output_file)

    def fake_separate(input_file, config):
        vocals_path = Path(input_file).with_name("vocals.wav")
        no_vocals_path = Path(input_file).with_name("no_vocals.wav")
        vocals_path.write_bytes(b"vocals")
        no_vocals_path.write_bytes(b"backing")
        return {"vocals": vocals_path, "no_vocals": no_vocals_path}

    with patch("timing_editor.extract_and_normalize_audio", side_effect=fake_extract), patch(
        "timing_editor.separate_vocals",
        side_effect=fake_separate,
    ), audio_file.open("rb") as fh:
        response = client.post(
            "/api/import",
            data={
                "audio_file": (fh, audio_filename),
                "lyrics_text": "hello\nworld",
            },
            content_type="multipart/form-data",
        )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["project_id"] == project_name
    session_payload = client.get("/api/session").get_json()
    assert session_payload["source_name"] == audio_filename
    assert session_payload["output_video_name"] == f"{project_name} (Kareoke).mp4"
    project_paths = _project_paths(_editor_paths(config_path), project_name)
    assert _default_output_video_path(project_paths).name == f"{project_name} (Kareoke).mp4"


def test_timing_editor_session_falls_back_to_lyrics_file(tmp_path: Path) -> None:
    temp_dir = tmp_path / "temp"
    output_dir = tmp_path / "output"
    logs_dir = tmp_path / "logs"
    input_dir = tmp_path / "input"
    temp_dir.mkdir()
    output_dir.mkdir()
    logs_dir.mkdir()
    input_dir.mkdir()

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, temp_dir, output_dir, logs_dir)

    state_path = temp_dir / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "mode": "manual",
                "status": "ready",
                "audio_source": str(input_dir / "song.mp3"),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (temp_dir / "lyrics.txt").write_text("line one\nline two", encoding="utf-8")

    app = create_app(config_path)
    app.testing = True
    client = app.test_client()

    session_payload = client.get("/api/session").get_json()
    assert session_payload["lyrics_text"] == "line one\nline two"


def test_timing_editor_session_uses_single_input_filename_when_state_has_old_sanitized_name(tmp_path: Path) -> None:
    temp_dir = tmp_path / "temp"
    output_dir = tmp_path / "output"
    logs_dir = tmp_path / "logs"
    input_dir = tmp_path / "input"
    temp_dir.mkdir()
    output_dir.mkdir()
    logs_dir.mkdir()
    input_dir.mkdir()

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, temp_dir, output_dir, logs_dir)

    (input_dir / "גזוז - חללית - מוזיקה ישראלית (youtube).mp3").write_bytes(b"fake mp3")
    (temp_dir / "state.json").write_text(
        json.dumps(
            {
                "mode": "manual",
                "status": "ready",
                "audio_source": str(input_dir / "youtube_.mp3"),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    app = create_app(config_path)
    app.testing = True
    client = app.test_client()

    session_payload = client.get("/api/session").get_json()
    assert session_payload["source_name"] == "גזוז - חללית - מוזיקה ישראלית (youtube).mp3"
    assert session_payload["output_video_name"] == "גזוז - חללית - מוזיקה ישראלית (youtube) (Kareoke).mp4"


def test_timing_editor_export_mp4_endpoint(tmp_path: Path) -> None:
    temp_dir = tmp_path / "temp"
    output_dir = tmp_path / "output"
    logs_dir = tmp_path / "logs"
    input_dir = tmp_path / "input"
    temp_dir.mkdir()
    output_dir.mkdir()
    logs_dir.mkdir()
    input_dir.mkdir()

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, temp_dir, output_dir, logs_dir)

    manifest_path = temp_dir / "subtitles_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "lines": [
                    {"id": "line_000", "text": "hello world", "start": 1.0, "end": 2.0, "word_ids": ["word_0000", "word_0001"]},
                ],
                "words": [
                    {"id": "word_0000", "text": "hello", "start": 1.0, "end": 1.4, "line_index": 0},
                    {"id": "word_0001", "text": "world", "start": 1.4, "end": 2.0, "line_index": 0},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (temp_dir / "timing_overrides.json").write_text(
        json.dumps({"global_offset": 0.0, "placed_word_count": 1, "lines": {}, "words": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    _write_wav(temp_dir / "audio.wav")

    app = create_app(config_path)
    app.testing = True
    client = app.test_client()

    ass_path = temp_dir / "subtitles.ass"
    ass_path.write_text("dummy", encoding="utf-8")
    (temp_dir / "no_vocals.wav").write_bytes(b"backing")
    output_video = output_dir / "karaoke.mp4"

    def fake_render(audio_path, subtitles_path, render_config, progress_callback=None):
        assert Path(audio_path) == temp_dir / "no_vocals.wav"
        assert subtitles_path == ass_path
        return output_video

    with patch("timing_editor._build_manual_subtitles", return_value=ass_path), patch(
        "timing_editor.render_video",
        side_effect=fake_render,
    ):
        response = client.post("/api/export/mp4")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["subtitles_ass"] == str(ass_path)
    assert payload["output_video"] == str(output_video)


def test_build_manual_subtitles_clamps_previous_line_before_next_line(tmp_path: Path) -> None:
    temp_dir = tmp_path / "temp"
    output_dir = tmp_path / "output"
    logs_dir = tmp_path / "logs"
    input_dir = tmp_path / "input"
    temp_dir.mkdir()
    output_dir.mkdir()
    logs_dir.mkdir()
    input_dir.mkdir()

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, temp_dir, output_dir, logs_dir)

    (temp_dir / "timing_editor_manifest.json").write_text(
        json.dumps(
            {
                "lines": [
                    {"id": "line_000", "text": "hello world", "word_ids": ["word_0000", "word_0001"]},
                    {"id": "line_001", "text": "more words", "word_ids": ["word_0002", "word_0003"]},
                ],
                "words": [
                    {"id": "word_0000", "index": 0, "text": "hello", "start": 1.0, "end": 1.4, "line_index": 0},
                    {"id": "word_0001", "index": 1, "text": "world", "start": 1.4, "end": 2.0, "line_index": 0},
                    {"id": "word_0002", "index": 2, "text": "more", "start": 1.8, "end": 2.2, "line_index": 1},
                    {"id": "word_0003", "index": 3, "text": "words", "start": 2.2, "end": 2.8, "line_index": 1},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (temp_dir / "timing_overrides.json").write_text(
        json.dumps({"version": 1, "placed_word_count": 4, "lines": {}, "words": {}}, ensure_ascii=False),
        encoding="utf-8",
    )

    paths = _editor_paths(config_path)
    ass_path = _build_manual_subtitles(paths, {"paths": {"temp_dir": str(temp_dir)}, "subtitle_builder": {"manifest_name": "subtitles_manifest.json", "output_ass_name": "subtitles.ass", "assets_dir_name": "subtitle_assets", "timing_overrides_name": "timing_overrides.json"}})
    assert ass_path == temp_dir / "subtitles.ass"

    manifest = json.loads((temp_dir / "subtitles_manifest.json").read_text(encoding="utf-8"))
    assert manifest["lines"][0]["end"] < manifest["lines"][1]["start"]


def test_timing_editor_open_output_endpoint(tmp_path: Path) -> None:
    temp_dir = tmp_path / "temp"
    output_dir = tmp_path / "output"
    logs_dir = tmp_path / "logs"
    input_dir = tmp_path / "input"
    temp_dir.mkdir()
    output_dir.mkdir()
    logs_dir.mkdir()
    input_dir.mkdir()

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, temp_dir, output_dir, logs_dir)

    output_video = output_dir / "karaoke.mp4"
    output_video.write_bytes(b"fake mp4")

    app = create_app(config_path)
    app.testing = True
    client = app.test_client()

    with patch.object(os, "startfile", autospec=True) as mock_startfile:
        response = client.post("/api/output/open")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["output_video"] == str(output_video)
    mock_startfile.assert_called_once_with(str(output_video))


def test_timing_editor_projects_list_and_select_named_project(tmp_path: Path) -> None:
    temp_dir = tmp_path / "temp"
    output_dir = tmp_path / "output"
    logs_dir = tmp_path / "logs"
    input_dir = tmp_path / "input"
    temp_dir.mkdir()
    output_dir.mkdir()
    logs_dir.mkdir()
    input_dir.mkdir()

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, temp_dir, output_dir, logs_dir)

    project_dir = temp_dir / "projects" / "My Song"
    project_dir.mkdir(parents=True)
    (project_dir / "state.json").write_text(
        json.dumps(
            {
                "mode": "manual",
                "status": "ready",
                "project_name": "My Song",
                "audio_source": str(input_dir / "track.mp3"),
                "word_count": 10,
                "line_count": 2,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    app = create_app(config_path)
    app.testing = True
    client = app.test_client()

    projects_payload = client.get("/api/projects").get_json()
    assert any(project["name"] == "My Song" for project in projects_payload["projects"])

    select_response = client.post("/api/projects/select", json={"project_id": "My Song"})
    assert select_response.status_code == 200
    session_payload = client.get("/api/session").get_json()
    assert session_payload["project_id"] == "My Song"
    assert session_payload["project_name"] == "My Song"


def test_timing_editor_create_empty_named_project(tmp_path: Path) -> None:
    temp_dir = tmp_path / "temp"
    output_dir = tmp_path / "output"
    logs_dir = tmp_path / "logs"
    input_dir = tmp_path / "input"
    temp_dir.mkdir()
    output_dir.mkdir()
    logs_dir.mkdir()
    input_dir.mkdir()

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, temp_dir, output_dir, logs_dir)

    app = create_app(config_path)
    app.testing = True
    client = app.test_client()

    response = client.post(
        "/api/projects/create",
        json={"project_name": "Empty Project", "lyrics_text": "hello\nworld"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["project_id"] == "Empty Project"

    project_paths = _project_paths(_editor_paths(config_path), "Empty Project")
    state = json.loads(project_paths.state_path.read_text(encoding="utf-8"))
    assert state["status"] == "empty"
    assert state["project_name"] == "Empty Project"
    assert state["lyrics_text"] == "hello\nworld"

    manifest = json.loads(project_paths.editor_manifest_path.read_text(encoding="utf-8"))
    assert [line["text"] for line in manifest["lines"]] == ["hello", "world"]
