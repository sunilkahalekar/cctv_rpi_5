import os
from iaqcam.fan_service import fan_token
def test_token_file(tmp_path, monkeypatch):
    monkeypatch.delenv("FAN_AGENT_TOKEN", raising=False)
    f = tmp_path / "t.txt"; f.write_text('"abc123"\r\n', encoding="utf-8-sig")
    assert fan_token({"token_env": "FAN_AGENT_TOKEN", "token_file": str(f)}) == "abc123"
    monkeypatch.setenv("FAN_AGENT_TOKEN", " envtok ")
    assert fan_token({"token_env": "FAN_AGENT_TOKEN", "token_file": str(f)}) == "envtok"
    monkeypatch.delenv("FAN_AGENT_TOKEN")
    assert fan_token({"token_file": str(tmp_path / "none.txt")}) == ""


def test_token_file_with_prefix(tmp_path, monkeypatch):
    monkeypatch.delenv("FAN_AGENT_TOKEN", raising=False)
    for line in ("FAN_AGENT_TOKEN=abc-_1", "set FAN_AGENT_TOKEN=abc-_1", "Bearer abc-_1", '"abc-_1"'):
        f = tmp_path / "t.txt"
        f.write_text(line + "\n")
        assert fan_token({"token_file": str(f)}) == "abc-_1", line
