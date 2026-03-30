"""Import and clean Hebrew lyrics from a source URL or local HTML/text file.

The primary use case is Shironet URL import. The importer is intentionally
defensive: it can clean raw HTML exports, local HTML files, or plain text, and
it raises a clear error if the remote site blocks direct HTTP access.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import requests
import yaml

LOGGER = logging.getLogger(__name__)

HEBREW_RE = re.compile(r"[\u0590-\u05FF]")


@dataclass(slots=True)
class LyricsImportResult:
    source: str
    provider: str
    text: str
    line_count: int


def load_config(config: str | Path | Mapping[str, Any]) -> dict[str, Any]:
    """Load pipeline configuration from a YAML file or mapping."""
    if isinstance(config, Mapping):
        return dict(config)

    config_path = Path(config)
    with config_path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Config file must contain a mapping: {config_path}")
    return loaded


def _paths_section(config: Mapping[str, Any]) -> Mapping[str, Any]:
    paths = config.get("paths")
    if not isinstance(paths, Mapping):
        raise KeyError("Missing 'paths' section in config")
    return paths


def _lyrics_section(config: Mapping[str, Any]) -> Mapping[str, Any]:
    settings = config.get("lyrics_source")
    if not isinstance(settings, Mapping):
        raise KeyError("Missing 'lyrics_source' section in config")
    return settings


def _is_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _read_source_text(source: str | Path, config: Mapping[str, Any]) -> tuple[str, str]:
    source_str = str(source)
    if _is_url(source_str):
        settings = _lyrics_section(config)
        headers = {"User-Agent": str(settings.get("user_agent", "Mozilla/5.0"))}
        timeout_seconds = int(settings.get("timeout_seconds", 30))
        response = requests.get(source_str, headers=headers, timeout=timeout_seconds)
        if response.status_code != 200:
            raise RuntimeError(
                f"Failed to fetch lyrics from {source_str}: HTTP {response.status_code}"
            )
        return response.text, source_str

    source_path = Path(source_str)
    if not source_path.exists():
        raise FileNotFoundError(f"Lyrics source does not exist: {source_path}")
    return source_path.read_text(encoding="utf-8"), str(source_path.resolve())


def _strip_html_to_text(content: str) -> str:
    content = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "\n", content)
    content = re.sub(r"(?i)<br\s*/?>", "\n", content)
    content = re.sub(r"(?i)</(p|div|li|tr|h[1-6])>", "\n", content)
    content = re.sub(r"(?is)<[^>]+>", " ", content)
    content = html.unescape(content)
    content = content.replace("\xa0", " ")
    return content


def _looks_like_html(content: str) -> bool:
    return "<" in content and ">" in content and "</" in content


def _clean_lines(lines: list[str]) -> list[str]:
    cleaned: list[str] = []
    for line in lines:
        normalized = re.sub(r"\s+", " ", line).strip()
        if not normalized:
            continue
        if len(normalized) <= 2 and not HEBREW_RE.search(normalized):
            continue
        cleaned.append(normalized)
    return cleaned


def extract_lyrics_text(raw_content: str) -> str:
    """Extract readable lyrics text from HTML or plain text."""
    content = _strip_html_to_text(raw_content) if _looks_like_html(raw_content) else raw_content
    lines = content.splitlines()
    cleaned_lines = _clean_lines(lines)

    # Collapse repeated duplicate lines while preserving stanza breaks.
    collapsed: list[str] = []
    previous = None
    for line in cleaned_lines:
        if line != previous:
            collapsed.append(line)
        previous = line
    return "\n".join(collapsed).strip()


def build_lyrics_settings(config: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize lyrics source settings."""
    settings = _lyrics_section(config)
    return {
        "enabled": bool(settings.get("enabled", False)),
        "provider": str(settings.get("provider", "shironet")),
        "source_url": str(settings.get("source_url", "")).strip(),
        "output_text_name": str(settings.get("output_text_name", "lyrics.txt")),
        "output_json_name": str(settings.get("output_json_name", "lyrics.json")),
    }


def import_lyrics(
    source: str | Path,
    config: str | Path | Mapping[str, Any],
) -> LyricsImportResult:
    """Import lyrics from a URL or local file and save cleaned artifacts under temp/."""
    config_data = load_config(config)
    settings = build_lyrics_settings(config_data)
    if not settings["enabled"] and not _is_url(str(source)) and not Path(str(source)).exists():
        raise ValueError("Lyrics source is disabled and no readable source was provided")

    paths = _paths_section(config_data)
    temp_dir = Path(paths["temp_dir"])
    temp_dir.mkdir(parents=True, exist_ok=True)

    start = time.perf_counter()
    LOGGER.info("lyrics_source:start source=%s", source)
    raw_content, resolved_source = _read_source_text(source, config_data)
    text = extract_lyrics_text(raw_content)
    if not text:
        raise ValueError(f"No lyrics text could be extracted from {resolved_source}")

    result = LyricsImportResult(
        source=resolved_source,
        provider=settings["provider"],
        text=text,
        line_count=len(text.splitlines()),
    )

    text_path = temp_dir / settings["output_text_name"]
    json_path = temp_dir / settings["output_json_name"]
    text_path.write_text(result.text, encoding="utf-8")
    json_path.write_text(
        json.dumps(
            {
                "source": result.source,
                "provider": result.provider,
                "line_count": result.line_count,
                "text": result.text,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    LOGGER.info(
        "lyrics_source:end text=%s json=%s duration_seconds=%.2f",
        text_path,
        json_path,
        time.perf_counter() - start,
    )
    return result


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import and clean Hebrew lyrics from a URL or local file.",
    )
    parser.add_argument("source", help="Lyrics URL or local HTML/text file")
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml (default: config.yaml)",
    )
    return parser


def main() -> int:
    parser = _build_arg_parser()
    args = parser.parse_args()
    import_lyrics(args.source, args.config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
