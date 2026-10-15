import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_locales_have_the_same_keys():
    text = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    assert "ui_lang" in text
    assert _keys(text, "ru") == _keys(text, "en")
    assert "download" in _keys(text, "en")
    assert {"progress", "waitingLink", "sending"} <= _keys(text, "en")
    assert "pendingRef" in text
    assert "file_progress" in text
    assert "randomUUID(" not in text
    assert "getRandomValues" in text


def _keys(text: str, lang: str) -> set[str]:
    match = re.search(rf"{lang}: \{{(.*?)\n  \}}", text, re.S)
    assert match, lang
    return set(re.findall(r"^\s+(\w+):", match.group(1), re.M))
