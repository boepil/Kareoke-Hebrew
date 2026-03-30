from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock, patch

from modules.lyrics_source import build_lyrics_settings, extract_lyrics_text, import_lyrics


def _config(tmp_dir: str = "temp") -> dict[str, object]:
    return {
        "paths": {"temp_dir": tmp_dir},
        "lyrics_source": {
            "enabled": True,
            "provider": "shironet",
            "source_url": "",
            "output_text_name": "lyrics.txt",
            "output_json_name": "lyrics.json",
            "user_agent": "Mozilla/5.0",
            "timeout_seconds": 30,
        },
    }


def test_extract_lyrics_text_cleans_html_and_preserves_lines() -> None:
    html = """
    <html>
      <body>
        <div>אחד</div>
        <div>שורה שניה</div>
        <script>ignore me</script>
        <div>שורה שלישית</div>
      </body>
    </html>
    """

    text = extract_lyrics_text(html)

    assert text.splitlines() == ["אחד", "שורה שניה", "שורה שלישית"]


def test_build_lyrics_settings_uses_config() -> None:
    settings = build_lyrics_settings(_config())

    assert settings["enabled"] is True
    assert settings["provider"] == "shironet"
    assert settings["output_text_name"] == "lyrics.txt"


def test_import_lyrics_writes_cleaned_artifacts_from_local_html(tmp_path: Path) -> None:
    html_path = tmp_path / "lyrics.html"
    html_path.write_text(
        """
        <html>
          <body>
            <div>תודה רבה</div>
            <div>תודה רבה</div>
            <div>עוד שורה</div>
          </body>
        </html>
        """,
        encoding="utf-8",
    )

    result = import_lyrics(html_path, _config(tmp_dir=str(tmp_path)))

    text_path = tmp_path / "lyrics.txt"
    json_path = tmp_path / "lyrics.json"
    assert text_path.exists()
    assert json_path.exists()
    assert text_path.read_text(encoding="utf-8").splitlines() == ["תודה רבה", "עוד שורה"]
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["line_count"] == 2
    assert result.line_count == 2


def test_import_lyrics_raises_on_blocked_remote_url(tmp_path: Path) -> None:
    response = Mock(status_code=403, text="blocked")
    with patch("modules.lyrics_source.requests.get", return_value=response):
        try:
            import_lyrics(
                "https://shironet.mako.co.il/artist?type=lyrics&lang=1&prfid=223&wrkid=3692",
                _config(tmp_dir=str(tmp_path)),
            )
        except RuntimeError as exc:
            assert "HTTP 403" in str(exc)
        else:
            raise AssertionError("Expected a RuntimeError for blocked URL")
