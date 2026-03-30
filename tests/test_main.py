from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from main import (
    STAGE_ALIGNER,
    STAGE_AUDIO,
    STAGE_RENDERER,
    STAGE_SEPARATOR,
    STAGE_SUBTITLES,
    STAGE_TRANSCRIBER,
    process,
)


def _write_config(path: Path, temp_dir: Path, output_dir: Path, logs_dir: Path) -> None:
    path.write_text(
        f"""paths:\n  input_dir: {temp_dir.parent / 'input'}\n  output_dir: {output_dir}\n  temp_dir: {temp_dir}\n  logs_dir: {logs_dir}\n\naudio_extractor:\n  ffmpeg_path: ffmpeg\n  sample_rate_hz: 44100\n  channels: 1\n  audio_codec: pcm_s16le\n  normalization_filter: loudnorm=I=-16:LRA=11:TP=-1.5\n\nseparator:\n  demucs_module: demucs.separate\n  model_name: htdemucs\n  two_stems: vocals\n  device: cpu\n  shifts: 0\n\ntranscriber:\n  model_name: large-v3\n  device: cpu\n  language: he\n  task: transcribe\n  fp16: false\n  audio_artifact: audio_wav\n  output_json_name: transcript.json\n  output_text_name: transcript.txt\n\nsubtitle_builder:\n  output_ass_name: subtitles.ass\n  font_name: Arial Unicode MS\n  font_size: 28\n  primary_color: \"&H00FFFFFF\"\n  secondary_color: \"&H0000FFFF\"\n  outline_color: \"&H00000000\"\n  back_color: \"&H7F000000\"\n  alignment: 2\n  margin_l: 40\n  margin_r: 40\n  margin_v: 28\n  outline: 2\n  shadow: 0\n\nrenderer:\n  ffmpeg_path: ffmpeg\n  audio_artifact: audio_wav\n  output_video_name: karaoke.mp4\n  video_size: 320x240\n  frame_rate: 30\n  background_color: black\n  video_codec: libx264\n  video_preset: medium\n  video_crf: 18\n  audio_codec: aac\n  audio_bitrate: 128k\n  faststart: true\n\naligner:\n  audio_artifact: audio_wav\n  device: cpu\n  compute_type: int8\n  output_json_name: aligned.json\n""",
        encoding="utf-8",
    )


def test_process_tracks_state_and_resumes(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    temp_dir = tmp_path / "temp"
    output_dir = tmp_path / "output"
    logs_dir = tmp_path / "logs"
    input_dir.mkdir()
    temp_dir.mkdir()
    output_dir.mkdir()
    logs_dir.mkdir()

    input_file = input_dir / "test.mp3"
    input_file.write_bytes(b"fake audio")
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, temp_dir, output_dir, logs_dir)

    audio_path = temp_dir / "audio.wav"
    vocals_path = temp_dir / "vocals.wav"
    no_vocals_path = temp_dir / "no_vocals.wav"
    transcript_json = temp_dir / "transcript.json"
    transcript_text = temp_dir / "transcript.txt"
    aligned_json = temp_dir / "aligned.json"
    subtitles_ass = temp_dir / "subtitles.ass"
    output_video = output_dir / "karaoke.mp4"

    def extract(*_args, **_kwargs):
        audio_path.write_bytes(b"audio")
        return audio_path

    def separate(*_args, **_kwargs):
        vocals_path.write_bytes(b"vocals")
        no_vocals_path.write_bytes(b"backing")
        return {"vocals": vocals_path, "no_vocals": no_vocals_path}

    def transcribe(audio_input, *_args, **_kwargs):
        assert Path(audio_input) == audio_path
        payload = {
            "source_file": str(audio_path),
            "model_name": "large-v3",
            "language": "he",
            "text": "שלום",
            "segments": [{"start": 0.0, "end": 1.0, "text": "שלום"}],
        }
        transcript_json.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        transcript_text.write_text("שלום", encoding="utf-8")
        return {"json_path": str(transcript_json), "text_path": str(transcript_text)}

    def align(audio_input, *_args, **_kwargs):
        assert Path(audio_input) == audio_path
        payload = {
            "language": "he",
            "segments": [
                {
                    "start": 0.0,
                    "end": 1.0,
                    "text": "שלום",
                    "words": [{"start": 0.0, "end": 1.0, "word": "שלום"}],
                }
            ],
        }
        aligned_json.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return {"json_path": str(aligned_json), "segments": payload["segments"], "language": "he"}

    def subtitles(*_args, **_kwargs):
        subtitles_ass.write_text("ass", encoding="utf-8")
        return subtitles_ass

    def render_fail(audio_input, *_args, **_kwargs):
        assert Path(audio_input) == audio_path
        raise RuntimeError("render failed")

    def render_ok(audio_input, *_args, **_kwargs):
        assert Path(audio_input) == audio_path
        output_video.write_bytes(b"mp4")
        return output_video

    with (
        patch("main.extract_and_normalize_audio", side_effect=extract),
        patch("main.separate_vocals", side_effect=separate),
        patch("main.transcribe_audio", side_effect=transcribe),
        patch("main.align_transcript", side_effect=align),
        patch("main.build_subtitles", side_effect=subtitles),
        patch("main.render_video", side_effect=render_fail),
    ):
        try:
            process(input_file, config_path)
        except RuntimeError as exc:
            assert "render failed" in str(exc)
        else:
            raise AssertionError("Expected the first render to fail")

    state_path = temp_dir / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["status"] == "failed"
    assert state["current_stage"] == STAGE_RENDERER
    assert state["completed_stages"] == [
        STAGE_AUDIO,
        STAGE_SEPARATOR,
        STAGE_TRANSCRIBER,
        STAGE_ALIGNER,
        STAGE_SUBTITLES,
    ]

    with (
        patch("main.extract_and_normalize_audio", side_effect=AssertionError("audio should be skipped")),
        patch("main.separate_vocals", side_effect=AssertionError("separator should be skipped")),
        patch("main.transcribe_audio", side_effect=AssertionError("transcriber should be skipped")),
        patch("main.align_transcript", side_effect=AssertionError("aligner should be skipped")),
        patch("main.build_subtitles", side_effect=AssertionError("subtitle builder should be skipped")),
        patch("main.render_video", side_effect=render_ok),
    ):
        output = process(input_file, config_path)

    assert output == output_video
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["status"] == "completed"
    assert state["completed_stages"] == [
        STAGE_AUDIO,
        STAGE_SEPARATOR,
        STAGE_TRANSCRIBER,
        STAGE_ALIGNER,
        STAGE_SUBTITLES,
        STAGE_RENDERER,
    ]
