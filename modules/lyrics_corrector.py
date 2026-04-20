"""Best-effort lyric correction using a local LM Studio endpoint.

This stage is intentionally optional. If the local OpenAI-compatible server is
unreachable, misconfigured, or returns an unusable payload, the original
transcript is returned unchanged and the pipeline keeps moving.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from pathlib import Path
from typing import Any, Mapping

import requests
import yaml

LOGGER = logging.getLogger(__name__)

DEFAULT_CORRECTION_ENDPOINT = "http://127.0.0.1:1234/v1/chat/completions"
DEFAULT_CORRECTION_MODEL = "qwen3-4b"
DEFAULT_CORRECTION_TIMEOUT_SECONDS = 90.0


def load_config(config: str | Path | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(config, Mapping):
        return dict(config)

    config_path = Path(config)
    with config_path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Config file must contain a mapping: {config_path}")
    return loaded


def _autosync_section(config: Mapping[str, Any]) -> Mapping[str, Any]:
    settings = config.get("autosync")
    if not isinstance(settings, Mapping):
        return {}
    return settings


def build_correction_settings(config: Mapping[str, Any]) -> dict[str, Any]:
    autosync = _autosync_section(config)
    endpoint_url = str(
        autosync.get("correction_endpoint_url")
        or autosync.get("lm_studio_endpoint_url")
        or DEFAULT_CORRECTION_ENDPOINT
    ).strip()
    model_id = str(
        autosync.get("correction_model_id")
        or autosync.get("lm_studio_model_id")
        or DEFAULT_CORRECTION_MODEL
    ).strip()
    timeout_seconds = float(autosync.get("correction_timeout_seconds", DEFAULT_CORRECTION_TIMEOUT_SECONDS))
    return {
        "enabled": bool(autosync.get("correction_enabled", True)),
        "endpoint_url": endpoint_url,
        "model_id": model_id,
        "timeout_seconds": max(timeout_seconds, 1.0),
    }


def _resolve_model_id(endpoint_url: str, preferred_model: str, timeout_seconds: float) -> str:
    if not endpoint_url:
        return preferred_model
    models_url = endpoint_url.replace("/chat/completions", "/models")
    try:
        response = requests.get(models_url, timeout=max(1.0, min(timeout_seconds, 8.0)))
        response.raise_for_status()
        payload = response.json()
        raw_models = payload.get("data", []) if isinstance(payload, Mapping) else []
        model_ids = [
            str(item.get("id", "")).strip()
            for item in raw_models
            if isinstance(item, Mapping) and str(item.get("id", "")).strip()
        ]
        if not model_ids:
            return preferred_model
        if preferred_model in model_ids:
            return preferred_model
        LOGGER.warning(
            "LM Studio model '%s' not found; using '%s' from /v1/models",
            preferred_model,
            model_ids[0],
        )
        return model_ids[0]
    except Exception:
        return preferred_model


def _read_transcript_source(transcript_source: str | Path | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(transcript_source, Mapping):
        return dict(transcript_source)

    transcript_path = Path(transcript_source)
    with transcript_path.open("r", encoding="utf-8") as handle:
        loaded = json.load(handle)
    if not isinstance(loaded, dict):
        raise ValueError(f"Transcript file must contain an object: {transcript_path}")
    return loaded


def _write_transcript_files(
    transcript: Mapping[str, Any],
    json_path: str | Path | None,
    text_path: str | Path | None,
) -> None:
    if json_path:
        json_target = Path(json_path)
        json_target.parent.mkdir(parents=True, exist_ok=True)
        json_target.write_text(json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")
    if text_path:
        text_target = Path(text_path)
        text_target.parent.mkdir(parents=True, exist_ok=True)
        text_target.write_text(str(transcript.get("text", "")), encoding="utf-8")


def _clean_text(text: str) -> str:
    compact = re.sub(r"\s+", " ", html_unescape(text)).strip()
    return compact


def html_unescape(text: str) -> str:
    from html import unescape

    return unescape(text or "")


def _extract_json_object(raw_text: str) -> dict[str, Any] | None:
    stripped = raw_text.strip()
    if not stripped:
        return None

    candidates = [stripped]
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        candidates.insert(0, fenced.group(1).strip())

    def _try_parse(candidate: str) -> dict[str, Any] | None:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict):
            return parsed
        return None

    for candidate in candidates:
        parsed = _try_parse(candidate)
        if parsed is not None:
            return parsed

        # Fallback: attempt to parse the first balanced JSON object in free text.
        start = candidate.find("{")
        while start != -1:
            depth = 0
            for end_index in range(start, len(candidate)):
                char = candidate[end_index]
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        sliced = candidate[start : end_index + 1]
                        parsed = _try_parse(sliced)
                        if parsed is not None:
                            return parsed
                        break
            start = candidate.find("{", start + 1)
    return None


def _normalize_segments(payload: Mapping[str, Any], fallback_transcript: Mapping[str, Any]) -> dict[str, Any] | None:
    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list):
        normalized = dict(fallback_transcript)
        normalized["text"] = _clean_text(str(payload.get("text", "") or fallback_transcript.get("text", "")))
        normalized["correction_provider"] = "lm_studio"
        normalized["correction_model_name"] = str(payload.get("model", "")).strip() or ""
        return normalized

    normalized_segments: list[dict[str, Any]] = []
    fallback_segments = fallback_transcript.get("segments", [])
    if not isinstance(fallback_segments, list):
        fallback_segments = []

    for index, raw_segment in enumerate(raw_segments):
        if not isinstance(raw_segment, Mapping):
            continue
        start = raw_segment.get("start")
        end = raw_segment.get("end")
        text = raw_segment.get("text")
        fallback_segment = fallback_segments[index] if index < len(fallback_segments) and isinstance(fallback_segments[index], Mapping) else {}
        if start is None:
            start = fallback_segment.get("start")
        if end is None:
            end = fallback_segment.get("end")
        if text is None:
            text = fallback_segment.get("text", "")
        if start is None or end is None:
            continue
        normalized_segments.append(
            {
                "id": raw_segment.get("id", fallback_segment.get("id", index)),
                "start": float(start),
                "end": float(end),
                "text": _clean_text(str(text)),
            }
        )

    if not normalized_segments and isinstance(fallback_segments, list):
        for index, fallback_segment in enumerate(fallback_segments):
            if not isinstance(fallback_segment, Mapping):
                continue
            start = fallback_segment.get("start")
            end = fallback_segment.get("end")
            text = fallback_segment.get("text", "")
            if start is None or end is None:
                continue
            normalized_segments.append(
                {
                    "id": fallback_segment.get("id", index),
                    "start": float(start),
                    "end": float(end),
                    "text": _clean_text(str(text)),
                }
            )

    normalized = dict(fallback_transcript)
    normalized["segments"] = normalized_segments
    normalized["text"] = _clean_text(str(payload.get("text", "") or " ".join(segment["text"] for segment in normalized_segments)))
    normalized["correction_provider"] = "lm_studio"
    normalized["correction_model_name"] = str(payload.get("model", "")).strip() or ""
    return normalized


def _request_with_fallback(
    endpoint_url: str,
    timeout_seconds: float,
    request_payload: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    headers = {"Accept": "application/json"}
    try:
        response = requests.post(
            endpoint_url,
            json=request_payload,
            headers=headers,
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        return response.json(), "json_schema"
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        if status_code is None or status_code >= 500:
            raise
        fallback_payload = dict(request_payload)
        fallback_payload.pop("response_format", None)
        body_preview = (exc.response.text or "").strip()[:400] if exc.response is not None else ""
        LOGGER.warning(
            "LM Studio correction retrying without response_format due to HTTP %s body=%s",
            status_code,
            body_preview,
        )
        fallback_response = requests.post(
            endpoint_url,
            json=fallback_payload,
            headers=headers,
            timeout=timeout_seconds,
        )
        fallback_response.raise_for_status()
        return fallback_response.json(), "plain_json"


def correct_transcript_with_lm_studio(
    transcript_source: str | Path | Mapping[str, Any],
    lyrics_text: str,
    config: str | Path | Mapping[str, Any],
) -> dict[str, Any]:
    """Attempt lyric-aware transcript correction, falling back silently on failure."""
    transcript = _read_transcript_source(transcript_source)
    lyrics_text = _clean_text(lyrics_text)
    if not lyrics_text:
        LOGGER.info("LM Studio correction skipped: no lyrics text")
        transcript["correction_status"] = "skipped"
        transcript["correction_reason"] = "no_lyrics_text"
        return transcript

    config_data = load_config(config)
    settings = build_correction_settings(config_data)
    settings["model_id"] = _resolve_model_id(
        str(settings["endpoint_url"]),
        str(settings["model_id"]),
        float(settings["timeout_seconds"]),
    )
    if not settings["enabled"] or not settings["endpoint_url"] or not settings["model_id"]:
        LOGGER.info(
            "LM Studio correction skipped: enabled=%s endpoint=%s model=%s",
            settings["enabled"],
            bool(settings["endpoint_url"]),
            bool(settings["model_id"]),
        )
        transcript["correction_status"] = "skipped"
        transcript["correction_reason"] = "disabled_or_unconfigured"
        return transcript

    fallback_transcript = copy.deepcopy(transcript)
    messages = [
        {
            "role": "system",
            "content": (
                "You correct Hebrew song transcription using reference lyrics. "
                "Preserve the original segment start/end timings exactly. "
                "Return only JSON with keys text and segments. "
                "segments must be a list of objects with start, end, and text. "
                "Use only the sung lyrics; no commentary."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "reference_lyrics": lyrics_text,
                    "rough_transcript": {
                        "language": transcript.get("language", "he"),
                        "text": transcript.get("text", ""),
                        "segments": transcript.get("segments", []),
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
        },
    ]

    request_payload = {
        "model": settings["model_id"],
        "messages": messages,
        "temperature": 0,
        "stream": False,
        "max_tokens": 512,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "lyrics_correction",
                "schema": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "segments": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {},
                                    "start": {"type": "number"},
                                    "end": {"type": "number"},
                                    "text": {"type": "string"},
                                },
                                "required": ["start", "end", "text"],
                                "additionalProperties": True,
                            },
                        },
                    },
                    "required": ["text", "segments"],
                    "additionalProperties": True,
                },
            },
        },
    }

    try:
        LOGGER.info(
            "LM Studio correction request: endpoint=%s model=%s timeout=%ss messages=%d",
            settings["endpoint_url"],
            settings["model_id"],
            settings["timeout_seconds"],
            len(messages),
        )
        payload, request_mode = _request_with_fallback(
            settings["endpoint_url"],
            settings["timeout_seconds"],
            request_payload,
        )
        choices = payload.get("choices", [])
        if not isinstance(choices, list) or not choices:
            LOGGER.info("LM Studio correction skipped: missing choices in response")
            transcript["correction_status"] = "skipped"
            transcript["correction_reason"] = "invalid_payload"
            return transcript
        message = choices[0].get("message", {}) if isinstance(choices[0], Mapping) else {}
        content = str(message.get("content", "")).strip()
        parsed = _extract_json_object(content)
        if not parsed:
            LOGGER.info("LM Studio correction skipped: response content was not JSON preview=%s", content[:400])
            transcript["correction_status"] = "skipped"
            transcript["correction_reason"] = "invalid_json_payload"
            return transcript
        normalized = _normalize_segments(parsed, fallback_transcript)
        if normalized is None:
            LOGGER.info("LM Studio correction skipped: normalized response was invalid")
            transcript["correction_status"] = "skipped"
            transcript["correction_reason"] = "invalid_segments"
            return transcript
        normalized["correction_request_mode"] = request_mode
        if not str(normalized.get("correction_model_name", "")).strip():
            normalized["correction_model_name"] = settings["model_id"]
        _write_transcript_files(
            normalized,
            normalized.get("json_path") or fallback_transcript.get("json_path"),
            normalized.get("text_path") or fallback_transcript.get("text_path"),
        )
        normalized["correction_status"] = "applied"
        return normalized
    except requests.ConnectionError:
        LOGGER.exception("LM Studio correction skipped: endpoint unreachable")
        transcript["correction_status"] = "skipped"
        transcript["correction_reason"] = "endpoint_unreachable"
        return transcript
    except requests.Timeout:
        LOGGER.exception("LM Studio correction skipped: request timeout")
        transcript["correction_status"] = "skipped"
        transcript["correction_reason"] = "request_timeout"
        return transcript
    except requests.HTTPError:
        LOGGER.exception("LM Studio correction skipped: unsupported response format or bad request")
        transcript["correction_status"] = "skipped"
        transcript["correction_reason"] = "unsupported_response_format"
        return transcript
    except Exception:
        LOGGER.exception("LM Studio correction skipped: request failed")
        transcript["correction_status"] = "skipped"
        transcript["correction_reason"] = "request_failed"
        return transcript
