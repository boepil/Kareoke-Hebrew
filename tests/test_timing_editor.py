from __future__ import annotations

import json
import os
import wave
from pathlib import Path
from unittest.mock import patch

from timing_editor import (
    _build_manual_subtitles,
    _ddg_pick_url_for_site,
    _ddg_search_lyrics_url,
    _default_pipeline_job,
    _default_output_video_path,
    _editor_paths,
    _discover_project_details_from_youtube,
    _fetch_shirrim_lyrics,
    _project_paths,
    _resolve_manual_word_windows,
    create_app,
)


def _write_config(path: Path, temp_dir: Path, output_dir: Path, logs_dir: Path) -> None:
    path.write_text(
        f"""paths:\n  input_dir: {temp_dir.parent / 'input'}\n  output_dir: {output_dir}\n  temp_dir: {temp_dir}\n  logs_dir: {logs_dir}\n\nsubtitle_builder:\n  manifest_name: subtitles_manifest.json\n  timing_overrides_name: timing_overrides.json
""",
        encoding="utf-8",
    )


def _ensure_test_subdirs(base: Path) -> None:
    (base / "subtitles").mkdir(parents=True, exist_ok=True)
    (base / "state").mkdir(parents=True, exist_ok=True)
    (base / "audio").mkdir(parents=True, exist_ok=True)
    (base / "transcripts").mkdir(parents=True, exist_ok=True)


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
    _ensure_test_subdirs(temp_dir)

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, temp_dir, output_dir, logs_dir)

    manifest_path = temp_dir / "subtitles" / "subtitles_manifest.json"
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
    (temp_dir / "state" / "timing_overrides.json").write_text(
        json.dumps({"global_offset": 0.0, "lines": {}, "words": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    _write_wav(temp_dir / "audio" / "audio.wav")

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

    on_disk = json.loads((temp_dir / "state" / "timing_overrides.json").read_text(encoding="utf-8"))
    assert on_disk["placed_word_count"] == 2
    assert on_disk["lyrics_text"] == "hello world\nmore words"
    assert on_disk["lines"]["line_000"]["end"] == 2.35
    assert on_disk["words"]["word_0001"]["end"] == 2.05
    assert (temp_dir / "subtitles" / "lyrics.txt").read_text(encoding="utf-8") == "hello world\nmore words"


def test_explicit_end_override_round_trips_through_save(tmp_path: Path) -> None:
    """Bug 5: _sanitize_override_body must preserve explicit_end.

    The front-end buildOverridesFromResolvedWords() sends explicit_end whenever
    a word has a user-set _explicitEnd (right-edge drag or shared boundary).
    The chain rule in syncResolvedWords Pass 2 uses _explicitEnd if defined
    and otherwise re-derives the end, causing the visible end to shift on
    reload if explicit_end is dropped on save.
    """
    temp_dir = tmp_path / "temp"
    output_dir = tmp_path / "output"
    logs_dir = tmp_path / "logs"
    input_dir = tmp_path / "input"
    temp_dir.mkdir()
    output_dir.mkdir()
    logs_dir.mkdir()
    input_dir.mkdir()
    _ensure_test_subdirs(temp_dir)

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, temp_dir, output_dir, logs_dir)

    (temp_dir / "subtitles" / "subtitles_manifest.json").write_text(
        json.dumps(
            {
                "lines": [
                    {
                        "id": "line_000",
                        "text": "hello world",
                        "start": 1.0,
                        "end": 2.0,
                        "word_ids": ["word_0000", "word_0001"],
                    },
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
    (temp_dir / "state" / "timing_overrides.json").write_text(
        json.dumps({"global_offset": 0.0, "lines": {}, "words": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    _write_wav(temp_dir / "audio" / "audio.wav")

    app = create_app(config_path)
    app.testing = True
    client = app.test_client()

    save_response = client.post(
        "/api/overrides",
        json={
            "global_offset": 0.0,
            "placed_word_count": 1,
            "lyrics_text": "hello world",
            "lines": {},
            "words": {
                "word_0000": {
                    "start": 1.0,
                    "end": 1.45,
                    "explicit_end": 1.45,
                },
            },
        },
    )
    assert save_response.status_code == 200
    saved_payload = save_response.get_json()
    assert saved_payload["words"]["word_0000"]["explicit_end"] == 1.45
    assert saved_payload["words"]["word_0000"]["end"] == 1.45

    get_response = client.get("/api/overrides")
    assert get_response.status_code == 200
    overrides = get_response.get_json()
    assert overrides["words"]["word_0000"]["explicit_end"] == 1.45

    on_disk = json.loads((temp_dir / "state" / "timing_overrides.json").read_text(encoding="utf-8"))
    assert on_disk["words"]["word_0000"]["explicit_end"] == 1.45


def test_sanitize_override_body_drops_unknown_keys() -> None:
    """Bug 5: explicit_end must be kept; unknown keys must still be dropped.

    Regression guard so future whitelist changes don't accidentally re-introduce
    the bug (e.g., by removing explicit_end again).
    """
    from timing_editor import _sanitize_override_body

    payload = _sanitize_override_body(
        {
            "global_offset": 0.0,
            "placed_word_count": 1,
            "lyrics_text": "x",
            "lines": {},
            "words": {
                "w1": {
                    "start": 1.0,
                    "end": 1.5,
                    "explicit_end": 1.5,
                    "junk_field": "should_be_dropped",
                    "another_junk": 42,
                },
            },
        }
    )

    assert payload["words"]["w1"]["explicit_end"] == 1.5
    assert "junk_field" not in payload["words"]["w1"]
    assert "another_junk" not in payload["words"]["w1"]


def test_timing_editor_import_creates_manual_project(tmp_path: Path) -> None:
    temp_dir = tmp_path / "temp"
    output_dir = tmp_path / "output"
    logs_dir = tmp_path / "logs"
    input_dir = tmp_path / "input"
    temp_dir.mkdir()
    output_dir.mkdir()
    logs_dir.mkdir()
    input_dir.mkdir()
    _ensure_test_subdirs(temp_dir)

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, temp_dir, output_dir, logs_dir)

    audio_file = tmp_path / "song.mp3"
    audio_file.write_bytes(b"fake mp3")

    app = create_app(config_path)
    app.testing = True
    client = app.test_client()

    def fake_extract(input_file, output_file, config):
        assert Path(input_file).name == "song.mp3"
        Path(output_file).parent.mkdir(parents=True, exist_ok=True)
        _write_wav(Path(output_file))
        return Path(output_file)

    def fake_separate(input_file, config):
        import pathlib
        pathlib.Path(input_file).parent.mkdir(parents=True, exist_ok=True)
        vocals_path = Path(input_file).with_name("vocals.wav")
        no_vocals_path = Path(input_file).with_name("no_vocals.wav")
        vocals_path.parent.mkdir(parents=True, exist_ok=True)
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

    assert response.status_code == 200, response.get_json()
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
    assert state["artifacts"]["vocals_wav"].endswith("vocals.wav")
    assert state["artifacts"]["no_vocals_wav"].endswith("no_vocals.wav")

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
    _ensure_test_subdirs(temp_dir)

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
        Path(output_file).parent.mkdir(parents=True, exist_ok=True)
        _write_wav(Path(output_file))
        return Path(output_file)

    def fake_separate(input_file, config):
        import pathlib
        pathlib.Path(input_file).parent.mkdir(parents=True, exist_ok=True)
        vocals_path = Path(input_file).with_name("vocals.wav")
        no_vocals_path = Path(input_file).with_name("no_vocals.wav")
        vocals_path.parent.mkdir(parents=True, exist_ok=True)
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

    assert response.status_code == 200, response.get_json()
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
    _ensure_test_subdirs(temp_dir)

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, temp_dir, output_dir, logs_dir)

    state_path = temp_dir / "state" / "state.json"
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
    (temp_dir / "subtitles" / "lyrics.txt").write_text("line one\nline two", encoding="utf-8")

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
    _ensure_test_subdirs(temp_dir)

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, temp_dir, output_dir, logs_dir)

    (input_dir / "גזוז - חללית - מוזיקה ישראלית (youtube).mp3").write_bytes(b"fake mp3")
    (temp_dir / "state" / "state.json").write_text(
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
    assert session_payload["output_video_name"] == "גזוז - חללית (Kareoke).mp4"


def test_timing_editor_pipeline_status_endpoint_reads_pipeline_state(tmp_path: Path) -> None:
    temp_dir = tmp_path / "temp"
    output_dir = tmp_path / "output"
    logs_dir = tmp_path / "logs"
    input_dir = tmp_path / "input"
    temp_dir.mkdir()
    output_dir.mkdir()
    logs_dir.mkdir()
    input_dir.mkdir()
    _ensure_test_subdirs(temp_dir)

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, temp_dir, output_dir, logs_dir)

    (temp_dir / "state" / "pipeline_state.json").write_text(
        json.dumps(
            {
                "version": 1,
                "job_id": "abc123",
                "status": "running",
                "project_id": "Song",
                "project_name": "Song",
                "current_stage_key": "gemma_transcription",
                "current_stage_label": "Gemma transcription",
                "stage_started_at": "2020-01-01T00:00:00Z",
                "stage_elapsed_seconds": 0.0,
                "started_at": "2020-01-01T00:00:00Z",
                "updated_at": "2020-01-01T00:00:05Z",
                "error": None,
                "stages": [
                    {
                        "key": "download_convert",
                        "label": "Download & convert",
                        "status": "done",
                        "detail": "",
                        "started_at": "2020-01-01T00:00:00Z",
                        "ended_at": "2020-01-01T00:00:01Z",
                        "elapsed_seconds": 1.0,
                    },
                    {
                        "key": "stem_separation",
                        "label": "Stem separation",
                        "status": "done",
                        "detail": "",
                        "started_at": "2020-01-01T00:00:01Z",
                        "ended_at": "2020-01-01T00:00:02Z",
                        "elapsed_seconds": 1.0,
                    },
                    {
                        "key": "gemma_transcription",
                        "label": "Gemma transcription",
                        "status": "running",
                        "detail": "Transcribing vocals",
                        "started_at": "2020-01-01T00:00:02Z",
                        "ended_at": None,
                        "elapsed_seconds": 0.0,
                    },
                    {
                        "key": "lm_studio_correction",
                        "label": "LM Studio correction",
                        "status": "pending",
                        "detail": "",
                        "started_at": None,
                        "ended_at": None,
                        "elapsed_seconds": 0.0,
                    },
                    {
                        "key": "whisperx_alignment",
                        "label": "WhisperX alignment",
                        "status": "pending",
                        "detail": "",
                        "started_at": None,
                        "ended_at": None,
                        "elapsed_seconds": 0.0,
                    },
                    {
                        "key": "editor_project",
                        "label": "Building editor project",
                        "status": "pending",
                        "detail": "",
                        "started_at": None,
                        "ended_at": None,
                        "elapsed_seconds": 0.0,
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    app = create_app(config_path)
    app.testing = True
    client = app.test_client()

    payload = client.get("/api/pipeline/status").get_json()
    assert payload["ok"] is True
    assert payload["status"] == "running"
    assert payload["current_stage_key"] == "gemma_transcription"
    assert payload["current_stage_label"] == "Gemma transcription"
    assert payload["stages"][2]["status"] == "running"
    assert payload["stages"][0]["status"] == "done"


def test_timing_editor_export_mp4_endpoint(tmp_path: Path) -> None:
    temp_dir = tmp_path / "temp"
    output_dir = tmp_path / "output"
    logs_dir = tmp_path / "logs"
    input_dir = tmp_path / "input"
    temp_dir.mkdir()
    output_dir.mkdir()
    logs_dir.mkdir()
    input_dir.mkdir()
    _ensure_test_subdirs(temp_dir)

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, temp_dir, output_dir, logs_dir)

    manifest_path = temp_dir / "subtitles" / "subtitles_manifest.json"
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
    (temp_dir / "state" / "timing_overrides.json").write_text(
        json.dumps({"global_offset": 0.0, "placed_word_count": 1, "lines": {}, "words": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    _write_wav(temp_dir / "audio" / "audio.wav")

    app = create_app(config_path)
    app.testing = True
    client = app.test_client()

    ass_path = temp_dir / "subtitles" / "subtitles.ass"
    ass_path.parent.mkdir(parents=True, exist_ok=True)
    ass_path.write_text("dummy", encoding="utf-8")
    (temp_dir / "audio" / "no_vocals.wav").write_bytes(b"backing")
    output_video = output_dir / "karaoke.mp4"

    def fake_render(audio_path, subtitles_path, render_config, progress_callback=None):
        assert Path(audio_path) == temp_dir / "audio" / "no_vocals.wav"
        assert subtitles_path == ass_path
        return output_video

    with patch("timing_editor._build_manual_subtitles", return_value=ass_path), patch(
        "timing_editor.render_video",
        side_effect=fake_render,
    ):
        response = client.post("/api/export/mp4")

    assert response.status_code == 200, response.get_json()
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
    _ensure_test_subdirs(temp_dir)

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, temp_dir, output_dir, logs_dir)

    (temp_dir / "subtitles" / "timing_editor_manifest.json").write_text(
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
    (temp_dir / "state" / "timing_overrides.json").write_text(
        json.dumps({"version": 1, "placed_word_count": 4, "lines": {}, "words": {}}, ensure_ascii=False),
        encoding="utf-8",
    )

    paths = _editor_paths(config_path)
    ass_path = _build_manual_subtitles(paths, {"paths": {"temp_dir": str(temp_dir)}, "subtitle_builder": {"manifest_name": "subtitles_manifest.json", "output_ass_name": "subtitles.ass", "assets_dir_name": "subtitle_assets", "timing_overrides_name": "timing_overrides.json"}})
    assert ass_path == temp_dir / "subtitles" / "subtitles.ass"

    manifest = json.loads((temp_dir / "subtitles" / "subtitles_manifest.json").read_text(encoding="utf-8"))
    assert manifest["lines"][0]["end"] < manifest["lines"][1]["start"]


def test_resolve_manual_word_windows_ignores_unplaced_words(tmp_path: Path) -> None:
    manifest = {
        "words": [
            {"id": "word_0000", "index": 0, "text": "hello", "start": 1.0, "end": 1.4, "line_index": 0},
            {"id": "word_0001", "index": 1, "text": "world", "start": 1.4, "end": 2.0, "line_index": 0},
            {"id": "word_0002", "index": 2, "text": "more", "start": 3.0, "end": 3.4, "line_index": 1},
            {"id": "word_0003", "index": 3, "text": "words", "start": 3.4, "end": 4.0, "line_index": 1},
        ]
    }

    windows = _resolve_manual_word_windows(
        manifest,
        {"version": 1, "placed_word_count": 2, "lines": {}, "words": {}},
    )

    assert set(windows) == {"word_0000", "word_0001"}


def test_build_manual_subtitles_skips_lines_after_reset_later(tmp_path: Path) -> None:
    temp_dir = tmp_path / "temp"
    output_dir = tmp_path / "output"
    logs_dir = tmp_path / "logs"
    input_dir = tmp_path / "input"
    temp_dir.mkdir()
    output_dir.mkdir()
    logs_dir.mkdir()
    input_dir.mkdir()
    _ensure_test_subdirs(temp_dir)

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, temp_dir, output_dir, logs_dir)

    (temp_dir / "subtitles" / "timing_editor_manifest.json").write_text(
        json.dumps(
            {
                "lines": [
                    {"id": "line_000", "text": "hello world", "word_ids": ["word_0000", "word_0001"]},
                    {"id": "line_001", "text": "more words", "word_ids": ["word_0002", "word_0003"]},
                ],
                "words": [
                    {"id": "word_0000", "index": 0, "text": "hello", "start": 1.0, "end": 1.4, "line_index": 0},
                    {"id": "word_0001", "index": 1, "text": "world", "start": 1.4, "end": 2.0, "line_index": 0},
                    {"id": "word_0002", "index": 2, "text": "more", "start": 3.0, "end": 3.4, "line_index": 1},
                    {"id": "word_0003", "index": 3, "text": "words", "start": 3.4, "end": 4.0, "line_index": 1},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (temp_dir / "state" / "timing_overrides.json").write_text(
        json.dumps({"version": 1, "placed_word_count": 2, "lines": {}, "words": {}}, ensure_ascii=False),
        encoding="utf-8",
    )

    paths = _editor_paths(config_path)
    _build_manual_subtitles(
        paths,
        {
            "paths": {"temp_dir": str(temp_dir)},
            "subtitle_builder": {
                "manifest_name": "subtitles_manifest.json",
                "output_ass_name": "subtitles.ass",
                "assets_dir_name": "subtitle_assets",
                "timing_overrides_name": "timing_overrides.json",
            },
        },
    )

    manifest = json.loads((temp_dir / "subtitles" / "subtitles_manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["lines"]) == 1
    assert manifest["lines"][0]["text"] == "hello world"


def test_timing_editor_open_output_endpoint(tmp_path: Path) -> None:
    temp_dir = tmp_path / "temp"
    output_dir = tmp_path / "output"
    logs_dir = tmp_path / "logs"
    input_dir = tmp_path / "input"
    temp_dir.mkdir()
    output_dir.mkdir()
    logs_dir.mkdir()
    input_dir.mkdir()
    _ensure_test_subdirs(temp_dir)

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, temp_dir, output_dir, logs_dir)

    output_video = output_dir / "karaoke.mp4"
    output_video.write_bytes(b"fake mp4")

    app = create_app(config_path)
    app.testing = True
    client = app.test_client()

    with patch.object(os, "startfile", autospec=True) as mock_startfile:
        response = client.post("/api/output/open")

    assert response.status_code == 200, response.get_json()
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
    _ensure_test_subdirs(temp_dir)

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, temp_dir, output_dir, logs_dir)

    project_dir = temp_dir / "projects" / "My Song"
    project_dir.mkdir(parents=True, exist_ok=True)
    _ensure_test_subdirs(project_dir)
    (project_dir / "state" / "state.json").write_text(
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
    _ensure_test_subdirs(temp_dir)

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, temp_dir, output_dir, logs_dir)

    app = create_app(config_path)
    app.testing = True
    client = app.test_client()

    response = client.post(
        "/api/projects/create",
        json={"project_name": "Empty Project", "lyrics_text": "hello\nworld"},
    )

    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert payload["project_id"] == "Empty Project"

    project_paths = _project_paths(_editor_paths(config_path), "Empty Project")
    state = json.loads(project_paths.state_path.read_text(encoding="utf-8"))
    assert state["status"] == "empty"
    assert state["project_name"] == "Empty Project"
    assert state["lyrics_text"] == "hello\nworld"

    manifest = json.loads(project_paths.editor_manifest_path.read_text(encoding="utf-8"))
    assert [line["text"] for line in manifest["lines"]] == ["hello", "world"]


def test_timing_editor_save_project_renames_project(tmp_path: Path) -> None:
    temp_dir = tmp_path / "temp"
    output_dir = tmp_path / "output"
    logs_dir = tmp_path / "logs"
    input_dir = tmp_path / "input"
    temp_dir.mkdir()
    output_dir.mkdir()
    logs_dir.mkdir()
    input_dir.mkdir()
    _ensure_test_subdirs(temp_dir)

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, temp_dir, output_dir, logs_dir)

    app = create_app(config_path)
    app.testing = True
    client = app.test_client()

    create_response = client.post(
        "/api/projects/create",
        json={"project_name": "Old Project", "lyrics_text": "hello\nworld"},
    )
    assert create_response.status_code == 200

    save_response = client.post(
        "/api/projects/save",
        json={
            "project_id": "Old Project",
            "project_name": "New Project",
            "global_offset": 0.0,
            "placed_word_count": 0,
            "lyrics_text": "hello\nworld",
            "lines": {},
            "words": {},
        },
    )
    assert save_response.status_code == 200
    payload = save_response.get_json()
    assert payload["project_id"] == "New Project"

    old_paths = _project_paths(_editor_paths(config_path), "Old Project")
    new_paths = _project_paths(_editor_paths(config_path), "New Project")
    assert not old_paths.temp_dir.exists()
    assert new_paths.state_path.exists()

    session_payload = client.get("/api/session").get_json()
    assert session_payload["project_id"] == "New Project"
    assert session_payload["project_name"] == "New Project"


def test_timing_editor_delete_project_removes_project(tmp_path: Path) -> None:
    temp_dir = tmp_path / "temp"
    output_dir = tmp_path / "output"
    logs_dir = tmp_path / "logs"
    input_dir = tmp_path / "input"
    temp_dir.mkdir()
    output_dir.mkdir()
    logs_dir.mkdir()
    input_dir.mkdir()
    _ensure_test_subdirs(temp_dir)

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, temp_dir, output_dir, logs_dir)

    app = create_app(config_path)
    app.testing = True
    client = app.test_client()

    create_response = client.post(
        "/api/projects/create",
        json={"project_name": "Delete Me", "lyrics_text": "hello"},
    )
    assert create_response.status_code == 200

    delete_response = client.post("/api/projects/delete", json={"project_id": "Delete Me"})
    assert delete_response.status_code == 200
    payload = delete_response.get_json()
    assert payload["deleted_project_id"] == "Delete Me"
    assert not _project_paths(_editor_paths(config_path), "Delete Me").temp_dir.exists()

    projects_payload = client.get("/api/projects").get_json()
    assert all(project["id"] != "Delete Me" for project in projects_payload["projects"])


def test_timing_editor_session_repairs_stale_manifest_when_unplaced(tmp_path: Path) -> None:
    temp_dir = tmp_path / "temp"
    output_dir = tmp_path / "output"
    logs_dir = tmp_path / "logs"
    input_dir = tmp_path / "input"
    temp_dir.mkdir()
    output_dir.mkdir()
    logs_dir.mkdir()
    input_dir.mkdir()
    _ensure_test_subdirs(temp_dir)

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, temp_dir, output_dir, logs_dir)

    project_paths = _project_paths(_editor_paths(config_path), "Repair Me")
    project_paths.temp_dir.mkdir(parents=True, exist_ok=True)
    project_paths.editor_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    project_paths.editor_manifest_path.write_text(
        json.dumps(
            {
                "lines": [{"id": "line_000", "index": 0, "text": "old song", "start": 0.0, "end": 2.0, "word_ids": ["word_0000", "word_0001"]}],
                "words": [
                    {"id": "word_0000", "index": 0, "line_index": 0, "text": "old", "start": 0.0, "end": 1.0},
                    {"id": "word_0001", "index": 1, "line_index": 0, "text": "song", "start": 1.0, "end": 2.0},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    project_paths.overrides_path.parent.mkdir(parents=True, exist_ok=True)
    project_paths.overrides_path.write_text(
        json.dumps(
            {"version": 1, "placed_word_count": 0, "lyrics_text": "new words here", "lines": {}, "words": {}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    project_paths.state_path.parent.mkdir(parents=True, exist_ok=True)
    project_paths.state_path.write_text(
        json.dumps(
            {"mode": "manual", "status": "empty", "project_name": "Repair Me", "lyrics_text": "new words here", "line_count": 1, "word_count": 2},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tmp_path / "data" / ".current_project").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / ".current_project").write_text("Repair Me", encoding="utf-8")

    app = create_app(config_path)
    app.testing = True
    client = app.test_client()

    session_payload = client.get("/api/session").get_json()
    assert session_payload["project_name"] == "Repair Me"
    manifest_payload = client.get("/api/manifest").get_json()
    assert [word["text"] for word in manifest_payload["words"]] == ["new", "words", "here"]


def test_timing_editor_attach_audio_to_existing_project(tmp_path: Path) -> None:
    temp_dir = tmp_path / "temp"
    output_dir = tmp_path / "output"
    logs_dir = tmp_path / "logs"
    input_dir = tmp_path / "input"
    temp_dir.mkdir()
    output_dir.mkdir()
    logs_dir.mkdir()
    input_dir.mkdir()
    _ensure_test_subdirs(temp_dir)

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, temp_dir, output_dir, logs_dir)

    app = create_app(config_path)
    app.testing = True
    client = app.test_client()

    create_response = client.post(
        "/api/projects/create",
        json={"project_name": "Attach Existing", "lyrics_text": "hello\nworld"},
    )
    assert create_response.status_code == 200

    audio_file = tmp_path / "song.mp3"
    audio_file.write_bytes(b"fake mp3")

    def fake_extract(input_file, output_file, config):
        import pathlib
        pathlib.Path(output_file).parent.mkdir(parents=True, exist_ok=True)
        Path(output_file).parent.mkdir(parents=True, exist_ok=True)
        _write_wav(Path(output_file))
        return Path(output_file)

    with patch("timing_editor.extract_and_normalize_audio", side_effect=fake_extract), audio_file.open("rb") as fh:
        attach_response = client.post(
            "/api/projects/attach-audio",
            data={
                "project_id": "Attach Existing",
                "audio_file": (fh, "song.mp3"),
            },
            content_type="multipart/form-data",
        )

    assert attach_response.status_code == 200
    project_paths = _project_paths(_editor_paths(config_path), "Attach Existing")
    state = json.loads(project_paths.state_path.read_text(encoding="utf-8"))
    assert state["status"] == "ready"
    assert state["source_name"] == "song.mp3"
    assert project_paths.audio_path.exists()


def test_timing_editor_import_youtube_creates_manual_project(tmp_path: Path) -> None:
    temp_dir = tmp_path / "temp"
    output_dir = tmp_path / "output"
    logs_dir = tmp_path / "logs"
    input_dir = tmp_path / "input"
    temp_dir.mkdir()
    output_dir.mkdir()
    logs_dir.mkdir()
    input_dir.mkdir()
    _ensure_test_subdirs(temp_dir)

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, temp_dir, output_dir, logs_dir)

    app = create_app(config_path)
    app.testing = True
    client = app.test_client()

    downloaded_audio = input_dir / "Downloaded Song [abc123].mp3"
    downloaded_audio.write_bytes(b"fake mp3")

    def fake_download(paths, youtube_url):
        assert youtube_url == "https://youtube.com/watch?v=abc123"
        return downloaded_audio

    def fake_extract(input_file, output_file, config):
        assert Path(input_file).name == "Downloaded Song.mp3"
        Path(output_file).parent.mkdir(parents=True, exist_ok=True)
        _write_wav(Path(output_file))
        return Path(output_file)

    with patch("timing_editor._download_youtube_audio", side_effect=fake_download), patch(
        "timing_editor._discover_project_details_from_youtube",
        return_value={},
    ), patch("timing_editor.extract_and_normalize_audio", side_effect=fake_extract):
        response = client.post(
            "/api/import/youtube",
            json={
                "youtube_url": "https://youtube.com/watch?v=abc123",
                "project_name": "Downloaded Song",
                "lyrics_text": "hello\nworld",
            },
        )

    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["project_id"] == "Downloaded Song"
    assert payload["project_name"] == "Downloaded Song"
    project_paths = _project_paths(_editor_paths(config_path), "Downloaded Song")
    state = json.loads(project_paths.state_path.read_text(encoding="utf-8"))
    assert state["status"] == "ready"
    assert state["project_name"] == "Downloaded Song"
    assert state["lyrics_text"] == "hello\nworld"


def test_timing_editor_import_youtube_attaches_to_existing_project(tmp_path: Path) -> None:
    temp_dir = tmp_path / "temp"
    output_dir = tmp_path / "output"
    logs_dir = tmp_path / "logs"
    input_dir = tmp_path / "input"
    temp_dir.mkdir()
    output_dir.mkdir()
    logs_dir.mkdir()
    input_dir.mkdir()
    _ensure_test_subdirs(temp_dir)

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, temp_dir, output_dir, logs_dir)

    app = create_app(config_path)
    app.testing = True
    client = app.test_client()

    create_response = client.post(
        "/api/projects/create",
        json={"project_name": "YouTube Attach", "lyrics_text": "hello\nworld"},
    )
    assert create_response.status_code == 200

    downloaded_audio = input_dir / "YouTube Song [xyz789].mp3"
    downloaded_audio.write_bytes(b"fake mp3")

    def fake_download(paths, youtube_url):
        assert youtube_url == "https://youtube.com/watch?v=xyz789"
        return downloaded_audio

    def fake_extract(input_file, output_file, config):
        assert Path(input_file).name == "YouTube Song.mp3"
        Path(output_file).parent.mkdir(parents=True, exist_ok=True)
        _write_wav(Path(output_file))
        return Path(output_file)

    with patch("timing_editor._download_youtube_audio", side_effect=fake_download), patch(
        "timing_editor._discover_project_details_from_youtube",
        return_value={},
    ), patch("timing_editor.extract_and_normalize_audio", side_effect=fake_extract):
        response = client.post(
            "/api/import/youtube",
            json={
                "project_id": "YouTube Attach",
                "youtube_url": "https://youtube.com/watch?v=xyz789",
            },
        )

    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["project_id"] == "YouTube Attach"
    assert payload["project_name"] == "YouTube Song"
    project_paths = _project_paths(_editor_paths(config_path), "YouTube Attach")
    state = json.loads(project_paths.state_path.read_text(encoding="utf-8"))
    assert state["project_name"] == "YouTube Song"
    assert state["source_name"] == "YouTube Song.mp3"


def test_timing_editor_import_youtube_auto_finds_lyrics_and_renames_project(tmp_path: Path) -> None:
    temp_dir = tmp_path / "temp"
    output_dir = tmp_path / "output"
    logs_dir = tmp_path / "logs"
    input_dir = tmp_path / "input"
    temp_dir.mkdir()
    output_dir.mkdir()
    logs_dir.mkdir()
    input_dir.mkdir()
    _ensure_test_subdirs(temp_dir)

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, temp_dir, output_dir, logs_dir)

    app = create_app(config_path)
    app.testing = True
    client = app.test_client()

    downloaded_audio = input_dir / "downloaded.mp3"
    downloaded_audio.write_bytes(b"fake mp3")

    def fake_download(paths, youtube_url):
        assert youtube_url == "https://youtube.com/watch?v=auto1"
        return downloaded_audio

    def fake_extract(input_file, output_file, config):
        assert Path(input_file).name == "\u05d0\u05d9\u05d9\u05dc \u05d2\u05d5\u05dc\u05df - \u05e9\u05dd \u05de\u05d9\u05d5\u05d8\u05d9\u05d5\u05d1.mp3"
        Path(output_file).parent.mkdir(parents=True, exist_ok=True)
        _write_wav(Path(output_file))
        return Path(output_file)

    with patch("timing_editor._download_youtube_audio", side_effect=fake_download), patch(
        "timing_editor._discover_project_details_from_youtube",
        return_value={
            "lyrics_text": "line one\nline two",
            "project_name": "\u05d0\u05d9\u05d9\u05dc \u05d2\u05d5\u05dc\u05df - \u05de\u05d9\u05dc\u05d9\u05d5\u05df \u05e9\u05d9\u05e8\u05d9\u05dd",
            "youtube_project_name": "\u05d0\u05d9\u05d9\u05dc \u05d2\u05d5\u05dc\u05df - \u05e9\u05dd \u05de\u05d9\u05d5\u05d8\u05d9\u05d5\u05d1",
            "lyrics_source_url": "https://shirrim.com/song-lyrics/example/",
            "lyrics_title": "\u05de\u05d9\u05dc\u05d9\u05dd \u05dc\u05e9\u05d9\u05e8 \u05d0\u05d9\u05d9\u05dc \u05d2\u05d5\u05dc\u05df - \u05de\u05d9\u05dc\u05d9\u05d5\u05df \u05e9\u05d9\u05e8\u05d9\u05dd",
        },
    ), patch("timing_editor.extract_and_normalize_audio", side_effect=fake_extract):
        response = client.post(
            "/api/import/youtube",
            json={"youtube_url": "https://youtube.com/watch?v=auto1"},
        )

    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert payload["project_id"] == "\u05d0\u05d9\u05d9\u05dc \u05d2\u05d5\u05dc\u05df - \u05e9\u05dd \u05de\u05d9\u05d5\u05d8\u05d9\u05d5\u05d1"
    assert payload["lyrics_source_url"] == "https://shirrim.com/song-lyrics/example/"
    assert payload["project_name"] == "\u05d0\u05d9\u05d9\u05dc \u05d2\u05d5\u05dc\u05df - \u05e9\u05dd \u05de\u05d9\u05d5\u05d8\u05d9\u05d5\u05d1"
    project_paths = _project_paths(_editor_paths(config_path), payload["project_id"])
    state = json.loads(project_paths.state_path.read_text(encoding="utf-8"))
    assert state["status"] == "ready"
    assert state["project_name"] == "\u05d0\u05d9\u05d9\u05dc \u05d2\u05d5\u05dc\u05df - \u05e9\u05dd \u05de\u05d9\u05d5\u05d8\u05d9\u05d5\u05d1"


def test_timing_editor_import_youtube_attached_project_is_renamed_from_match(tmp_path: Path) -> None:
    temp_dir = tmp_path / "temp"
    output_dir = tmp_path / "output"
    logs_dir = tmp_path / "logs"
    input_dir = tmp_path / "input"
    temp_dir.mkdir()
    output_dir.mkdir()
    logs_dir.mkdir()
    input_dir.mkdir()
    _ensure_test_subdirs(temp_dir)

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, temp_dir, output_dir, logs_dir)

    app = create_app(config_path)
    app.testing = True
    client = app.test_client()

    create_response = client.post(
        "/api/projects/create",
        json={"project_name": "Temporary Name", "lyrics_text": ""},
    )
    assert create_response.status_code == 200

    downloaded_audio = input_dir / "downloaded.mp3"
    downloaded_audio.write_bytes(b"fake mp3")

    def fake_download(paths, youtube_url):
        assert youtube_url == "https://youtube.com/watch?v=auto2"
        return downloaded_audio

    def fake_extract(input_file, output_file, config):
        assert Path(input_file).name == "\u05e9\u05dd \u05d4\u05e9\u05d9\u05e8 - \u05e9\u05dd \u05d4\u05d6\u05de\u05e8.mp3"
        Path(output_file).parent.mkdir(parents=True, exist_ok=True)
        _write_wav(Path(output_file))
        return Path(output_file)

    with patch("timing_editor._download_youtube_audio", side_effect=fake_download), patch(
        "timing_editor._discover_project_details_from_youtube",
        return_value={
            "lyrics_text": "first line\nsecond line",
            "project_name": "\u05e9\u05dd \u05d4\u05e9\u05d9\u05e8 - \u05e9\u05dd \u05d4\u05d6\u05de\u05e8",
            "youtube_project_name": "\u05e9\u05dd \u05d4\u05e9\u05d9\u05e8 - \u05e9\u05dd \u05d4\u05d6\u05de\u05e8",
        },
    ), patch("timing_editor.extract_and_normalize_audio", side_effect=fake_extract):
        response = client.post(
            "/api/import/youtube",
            json={
                "project_id": "Temporary Name",
                "youtube_url": "https://youtube.com/watch?v=auto2",
            },
        )

    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert payload["project_id"] == "Temporary Name"
    assert payload["project_name"] == "\u05e9\u05dd \u05d4\u05e9\u05d9\u05e8 - \u05e9\u05dd \u05d4\u05d6\u05de\u05e8"
    project_paths = _project_paths(_editor_paths(config_path), "Temporary Name")
    state = json.loads(project_paths.state_path.read_text(encoding="utf-8"))
    assert state["project_name"] == "\u05e9\u05dd \u05d4\u05e9\u05d9\u05e8 - \u05e9\u05dd \u05d4\u05d6\u05de\u05e8"


def test_timing_editor_import_audio_auto_uses_filename_and_fetches_lyrics(tmp_path: Path) -> None:
    temp_dir = tmp_path / "temp"
    output_dir = tmp_path / "output"
    logs_dir = tmp_path / "logs"
    input_dir = tmp_path / "input"
    temp_dir.mkdir()
    output_dir.mkdir()
    logs_dir.mkdir()
    input_dir.mkdir()
    _ensure_test_subdirs(temp_dir)

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, temp_dir, output_dir, logs_dir)

    app = create_app(config_path)
    app.testing = True
    client = app.test_client()

    audio_file = tmp_path / "\u05d0\u05d9\u05d9\u05dc \u05d2\u05d5\u05dc\u05df - \u05e1\u05d8\u05dc\u05d5\u05ea.mp3"
    audio_file.write_bytes(b"fake mp3")

    def fake_extract(input_file, output_file, config):
        assert Path(input_file).name == audio_file.name
        Path(output_file).parent.mkdir(parents=True, exist_ok=True)
        _write_wav(Path(output_file))
        return Path(output_file)

    with patch(
        "timing_editor._discover_project_details_from_audio_filename",
        return_value={
            "project_name": "\u05d0\u05d9\u05d9\u05dc \u05d2\u05d5\u05dc\u05df - \u05e1\u05d8\u05dc\u05d5\u05ea",
            "lyrics_text": "line one\nline two",
            "lyrics_source_url": "https://shirrim.com/song-lyrics/example/",
            "lyrics_title": "\u05e1\u05d8\u05dc\u05d5\u05ea",
            "source_query": "\u05e1\u05d8\u05dc\u05d5\u05ea",
            "source_artist": "\u05d0\u05d9\u05d9\u05dc \u05d2\u05d5\u05dc\u05df",
        },
    ), patch("timing_editor.extract_and_normalize_audio", side_effect=fake_extract):
        with audio_file.open("rb") as fh:
            response = client.post(
                "/api/import/audio-auto",
                data={"audio_file": (fh, audio_file.name)},
                content_type="multipart/form-data",
            )

    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["project_id"] == "\u05d0\u05d9\u05d9\u05dc \u05d2\u05d5\u05dc\u05df - \u05e1\u05d8\u05dc\u05d5\u05ea"
    assert payload["lyrics_found"] is True
    assert payload["lyrics_source_url"] == "https://shirrim.com/song-lyrics/example/"
    assert payload["lyrics_title"] == "\u05e1\u05d8\u05dc\u05d5\u05ea"

    session_payload = client.get("/api/session").get_json()
    assert session_payload["project_name"] == "\u05d0\u05d9\u05d9\u05dc \u05d2\u05d5\u05dc\u05df - \u05e1\u05d8\u05dc\u05d5\u05ea"
    assert session_payload["lyrics_source_url"] == "https://shirrim.com/song-lyrics/example/"


def test_timing_editor_ai_first_pass_endpoint_runs_on_current_project(tmp_path: Path) -> None:
    temp_dir = tmp_path / "temp"
    output_dir = tmp_path / "output"
    logs_dir = tmp_path / "logs"
    input_dir = tmp_path / "input"
    temp_dir.mkdir()
    output_dir.mkdir()
    logs_dir.mkdir()
    input_dir.mkdir()
    _ensure_test_subdirs(temp_dir)

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, temp_dir, output_dir, logs_dir)

    app = create_app(config_path)
    app.testing = True
    client = app.test_client()

    audio_file = tmp_path / "song.mp3"
    audio_file.write_bytes(b"fake mp3")

    def fake_extract(input_file, output_file, config):
        assert Path(input_file).name == audio_file.name
        Path(output_file).parent.mkdir(parents=True, exist_ok=True)
        _write_wav(Path(output_file))
        return Path(output_file)

    with patch(
        "timing_editor._discover_project_details_from_audio_filename",
        return_value={"project_name": "song", "lyrics_text": "line one", "lyrics_source_url": ""},
    ), patch("timing_editor.extract_and_normalize_audio", side_effect=fake_extract):
        with audio_file.open("rb") as fh:
            create_response = client.post(
                "/api/import/audio-auto",
                data={"audio_file": (fh, audio_file.name)},
                content_type="multipart/form-data",
            )
    assert create_response.status_code == 200

    called = {}

    def fake_autosync(input_file, config, **kwargs):
        called["input_file"] = Path(input_file).name
        called["kwargs"] = kwargs
        return {
            "project": {
                "project_id": "song",
                "project_name": "song",
                "project_dir": str(temp_dir / "projects" / "song"),
                "state_path": str(temp_dir / "projects" / "song" / "state.json"),
                "manifest_path": str(temp_dir / "projects" / "song" / "timing_editor_manifest.json"),
                "overrides_path": str(temp_dir / "projects" / "song" / "timing_overrides.json"),
                "state": {"status": "ready", "project_name": "song", "source_name": "song.mp3", "lyrics_text": "line one"},
            }
        }

    with patch("timing_editor.run_first_pass_autosync", side_effect=fake_autosync):
        response = client.post("/api/pipeline/first-pass")

    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["status"] == "completed"
    assert called["input_file"] == "song.mp3"
    assert called["kwargs"]["project_name"] == "song"


def test_timing_editor_resets_stale_pipeline_job_and_allows_import(tmp_path: Path) -> None:
    temp_dir = tmp_path / "temp"
    output_dir = tmp_path / "output"
    logs_dir = tmp_path / "logs"
    input_dir = tmp_path / "input"
    temp_dir.mkdir()
    output_dir.mkdir()
    logs_dir.mkdir()
    input_dir.mkdir()
    _ensure_test_subdirs(temp_dir)

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, temp_dir, output_dir, logs_dir)

    stale_job = {
        "version": 1,
        "job_id": "stale",
        "status": "running",
        "project_id": "",
        "project_name": "",
        "current_stage_key": "",
        "current_stage_label": "",
        "stage_started_at": None,
        "stage_elapsed_seconds": 0.0,
        "started_at": None,
        "updated_at": None,
        "error": None,
        "stages": [
            {"key": "download_convert", "label": "Download & convert", "status": "pending", "detail": "", "started_at": None, "ended_at": None, "elapsed_seconds": 0.0},
            {"key": "stem_separation", "label": "Stem separation", "status": "pending", "detail": "", "started_at": None, "ended_at": None, "elapsed_seconds": 0.0},
        ],
    }
    (temp_dir / "state" / "pipeline_state.json").write_text(json.dumps(stale_job, ensure_ascii=False), encoding="utf-8")

    app = create_app(config_path)
    app.testing = True
    client = app.test_client()

    status_payload = client.get("/api/pipeline/status").get_json()
    assert status_payload["status"] == "idle"

    downloaded_audio = temp_dir / "downloaded.mp3"
    downloaded_audio.write_bytes(b"fake mp3")

    def fake_download(base_paths, youtube_url):
        return downloaded_audio

    def fake_extract(input_file, output_file, config):
        import pathlib
        pathlib.Path(output_file).parent.mkdir(parents=True, exist_ok=True)
        Path(output_file).parent.mkdir(parents=True, exist_ok=True)
        _write_wav(Path(output_file))
        return Path(output_file)

    with patch("timing_editor._download_youtube_audio", side_effect=fake_download), patch(
        "timing_editor._discover_project_details_from_youtube",
        return_value={"youtube_project_name": "song", "lyrics_text": "line one", "lyrics_source_url": ""},
    ), patch("timing_editor.extract_and_normalize_audio", side_effect=fake_extract):
        response = client.post("/api/import/youtube", json={"youtube_url": "https://youtube.com/watch?v=abc"})

    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert payload["ok"] is True


def test_timing_editor_pipeline_stop_terminates_worker_and_resets_state(tmp_path: Path) -> None:
    temp_dir = tmp_path / "temp"
    output_dir = tmp_path / "output"
    logs_dir = tmp_path / "logs"
    input_dir = tmp_path / "input"
    temp_dir.mkdir()
    output_dir.mkdir()
    logs_dir.mkdir()
    input_dir.mkdir()
    _ensure_test_subdirs(temp_dir)

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, temp_dir, output_dir, logs_dir)

    app = create_app(config_path)
    app.testing = True
    client = app.test_client()

    running_job = _default_pipeline_job()
    running_job.update(
        {
            "job_id": "active",
            "status": "running",
            "project_id": "song",
            "project_name": "song",
            "current_stage_key": "gemma_transcription",
            "current_stage_label": "Gemma transcription",
            "started_at": "2026-04-05T12:00:00Z",
            "updated_at": "2026-04-05T12:00:01Z",
            "stages": _default_pipeline_job()["stages"],
        }
    )
    (temp_dir / "state" / "pipeline_state.json").write_text(json.dumps(running_job, ensure_ascii=False), encoding="utf-8")
    app.config["PIPELINE_JOB"] = running_job

    class FakeProcess:
        pid = 4321

        def __init__(self) -> None:
            self.joined = False

        def is_alive(self) -> bool:
            return True

        def join(self, timeout: float | None = None) -> None:  # noqa: ARG002
            self.joined = True

    fake_process = FakeProcess()
    app.config["PIPELINE_WORKER"] = {
        "kind": "ai_first_pass",
        "job_id": "active",
        "process": fake_process,
        "project_key": "song",
        "project_name": "song",
    }

    with patch("timing_editor._terminate_process_tree") as terminate_tree:
        response = client.post("/api/pipeline/stop")

    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["status"] == "idle"
    assert payload["stopped"] is True
    terminate_tree.assert_called_once_with(4321)
    assert fake_process.joined is True
    assert app.config["PIPELINE_WORKER"] is None

    on_disk = json.loads((temp_dir / "state" / "pipeline_state.json").read_text(encoding="utf-8"))
    assert on_disk["status"] == "idle"


def test_timing_editor_youtube_project_name_uses_title_only_and_strips_suffixes() -> None:
    with patch(
        "timing_editor._fetch_youtube_metadata",
        return_value={
            "title": "\u05e0\u05d0\u05d5\u05e8 \u05db\u05d4\u05df \u2013 \u05e1\u05d8\u05dc\u05d5\u05ea (קליפ)",
            "uploader": "Channel Name",
            "channel": "Channel Name",
            "artist": "Channel Name",
        },
    ), patch("timing_editor._direct_search_shironet", return_value={"success": False, "lyrics_text": "", "source_url": ""}), patch("timing_editor._search_shirrim_results", return_value=[]):
        details = _discover_project_details_from_youtube(
            "https://youtube.com/watch?v=titleonly",
            "fallback-name.mp3",
        )

    assert details["youtube_title"] == "\u05e0\u05d0\u05d5\u05e8 \u05db\u05d4\u05df \u2013 \u05e1\u05d8\u05dc\u05d5\u05ea (קליפ)"
    assert details["youtube_project_name"] == "\u05e0\u05d0\u05d5\u05e8 \u05db\u05d4\u05df - \u05e1\u05d8\u05dc\u05d5\u05ea"


def test_timing_editor_youtube_project_name_falls_back_to_filename_when_title_missing() -> None:
    with patch(
        "timing_editor._fetch_youtube_metadata",
        return_value={
            "title": "",
            "uploader": "Channel Name",
            "channel": "Channel Name",
            "artist": "Channel Name",
        },
    ), patch("timing_editor._direct_search_shironet", return_value={"success": False, "lyrics_text": "", "source_url": ""}), patch("timing_editor._search_shirrim_results", return_value=[]):
        details = _discover_project_details_from_youtube(
            "https://youtube.com/watch?v=titlemissing",
            "Fallback Song [abc123].mp3",
        )

    assert details["youtube_project_name"] == "Fallback Song"
    assert details["youtube_project_name"] != "Channel Name"


def test_timing_editor_youtube_lyrics_search_does_not_fallback_to_wrong_song() -> None:
    with patch(
        "timing_editor._fetch_youtube_metadata",
        return_value={
            "title": "\u05d6\u05de\u05e0\u05da \u05e2\u05d1\u05e8 \u2013 \u05d0\u05d4\u05d5\u05d3 \u05d1\u05e0\u05d0\u05d9",
            "uploader": "Official Channel",
            "channel": "Official Channel",
            "artist": "אהוד בנאי",
        },
    ), patch(
        "timing_editor._direct_search_shironet",
        return_value={"success": False, "lyrics_text": "", "source_url": ""},
    ), patch(
        "timing_editor._search_shirrim_results",
        return_value=[
            {
                "title": "שיר אחר לגמרי - זמר אחר",
                "url": "https://shirrim.com/song-lyrics/different-song/",
                "query": "זמנך עבר",
            }
        ],
    ), patch("timing_editor._fetch_shirrim_lyrics") as mock_fetch:
        details = _discover_project_details_from_youtube(
            "https://youtube.com/watch?v=wrong-match",
            "fallback-name.mp3",
        )

    assert details["lyrics_text"] == ""
    assert details["lyrics_source_url"] == ""
    assert details["lyrics_title"] == ""
    mock_fetch.assert_not_called()


def test_fetch_shirrim_lyrics_follows_chords_page_to_lyrics() -> None:
    chords_html = """
    <html><body>
      <a href="https://shirrim.com/song-lyrics/example-song/">למילים של השיר זמנך עבר</a>
    </body></html>
    """
    lyrics_html = """
    <html><body>
      <h1 class="elementor-heading-title">מילים לשיר זמנך עבר - אהוד בנאי</h1>
      <div class="jet-listing-dynamic-field__content">
        המילים של השיר:
        <p>שורה ראשונה</p>
        <p>שורה שניה</p>
      </div>
    </body></html>
    """

    class _Response:
        def __init__(self, text: str) -> None:
            self.text = text

        def raise_for_status(self) -> None:
            return None

    with patch("timing_editor.requests.get", side_effect=[_Response(chords_html), _Response(lyrics_html)]) as mock_get:
        payload = _fetch_shirrim_lyrics("https://shirrim.com/song-chrods/example-song/")

    assert payload["source_url"] == "https://shirrim.com/song-lyrics/example-song/"
    assert payload["lyrics"] == "שורה ראשונה\n\nשורה שניה"
    assert payload["title"] == "מילים לשיר זמנך עבר - אהוד בנאי"
    assert mock_get.call_count == 2


def test_timing_editor_rejects_non_youtube_url_in_youtube_import(tmp_path: Path) -> None:
    temp_dir = tmp_path / "temp"
    output_dir = tmp_path / "output"
    logs_dir = tmp_path / "logs"
    input_dir = tmp_path / "input"
    temp_dir.mkdir()
    output_dir.mkdir()
    logs_dir.mkdir()
    input_dir.mkdir()
    _ensure_test_subdirs(temp_dir)

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, temp_dir, output_dir, logs_dir)

    app = create_app(config_path)
    app.testing = True
    client = app.test_client()

    response = client.post(
        "/api/import/youtube",
        json={"youtube_url": "https://shirrim.com/song-lyrics/example/"},
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert "youtube.com or youtu.be" in payload["error"]


# ----- DuckDuckGo-based lyrics URL discovery tests -----


def test_ddg_pick_url_for_site_matches_target() -> None:
    """Returns the first result whose URL belongs to the target site."""
    results = [
        {"title": "nagnu page", "href": "https://www.nagnu.co.il/x/y", "body": ""},
        {"title": "shirrim page", "href": "https://www.shirrim.com/song-lyrics/x/", "body": ""},
    ]
    assert _ddg_pick_url_for_site("nagnu.co.il", results) == "https://www.nagnu.co.il/x/y"
    assert _ddg_pick_url_for_site("shirrim.com", results) == "https://www.shirrim.com/song-lyrics/x/"


def test_ddg_pick_url_for_site_returns_none_when_no_match() -> None:
    """Returns None when no result is on the target site (does not fall back to other sites)."""
    results = [
        {"title": "nagnu page", "href": "https://www.nagnu.co.il/x/y", "body": ""},
        {"title": "shironet", "href": "https://shironet.mako.co.il/artist?type=lyrics", "body": ""},
    ]
    assert _ddg_pick_url_for_site("example.com", results) is None
    assert _ddg_pick_url_for_site("shirrim.com", results) is None


def test_ddg_pick_url_for_site_empty_results() -> None:
    """Returns None on empty input."""
    assert _ddg_pick_url_for_site("shirrim.com", []) is None


def test_ddg_search_lyrics_url_handles_missing_package(monkeypatch) -> None:
    """When ddgs is not importable, returns [] without raising."""
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "ddgs":
            raise ImportError("No module named 'ddgs'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert _ddg_search_lyrics_url("שיר כלשהו", "זמר כלשהו") == []


def test_ddg_search_lyrics_url_handles_empty_input() -> None:
    """When song and artist are both empty, returns [] without calling ddgs."""
    assert _ddg_search_lyrics_url("", "") == []
    assert _ddg_search_lyrics_url("  ", "") == []
