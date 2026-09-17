import pytest
import json
from datetime import datetime

import gather
from gather import JST, parse_json_safely, extract_text_from_gemini_response, wait_until_notify_time

def test_parse_json_safely_pure_json():
    text = '[{"id": 1, "title": "Test"}]'
    res = parse_json_safely(text)
    assert isinstance(res, list)
    assert res[0]["title"] == "Test"

def test_parse_json_safely_with_markdown():
    text = "```json\n[{\"id\": 1}]\n```"
    res = parse_json_safely(text)
    assert res == [{"id": 1}]

def test_parse_json_safely_with_prefix_suffix():
    text = "以下が要求されたJSONです：\n```json\n[{\"id\": 2}]\n```\nよろしくお願いします。"
    res = parse_json_safely(text)
    assert res == [{"id": 2}]

def test_parse_json_safely_object():
    text = "```json\n{\"error\": \"failed\"}\n```"
    res = parse_json_safely(text)
    assert res == {"error": "failed"}

def test_parse_json_safely_invalid_json():
    text = "これはJSONではありません"
    res = parse_json_safely(text)
    assert res is None

def test_extract_text_from_gemini_response_valid():
    data = {
        "candidates": [
            {
                "finishReason": "STOP",
                "content": {
                    "parts": [{"text": "Hello world"}]
                }
            }
        ]
    }
    assert extract_text_from_gemini_response(data) == "Hello world"

def test_extract_text_from_gemini_response_invalid_finish_reason():
    data = {
        "candidates": [
            {
                "finishReason": "SAFETY",
                "content": {
                    "parts": [{"text": "Blocked"}]
                }
            }
        ]
    }
    assert extract_text_from_gemini_response(data) is None

def test_extract_text_from_gemini_response_malformed():
    data = {"unexpected": "format"}
    assert extract_text_from_gemini_response(data) is None

@pytest.fixture
def sleeps(monkeypatch):
    calls = []
    monkeypatch.setattr(gather.time, "sleep", lambda sec: calls.append(sec))
    return calls

def test_wait_until_notify_time_unset(monkeypatch, sleeps):
    monkeypatch.delenv("NOTIFY_AT_JST", raising=False)
    now = datetime(2026, 9, 14, 4, 30, tzinfo=JST)
    assert wait_until_notify_time(now) == 0.0
    assert sleeps == []

def test_wait_until_notify_time_before(monkeypatch, sleeps):
    monkeypatch.setenv("NOTIFY_AT_JST", "07:00")
    now = datetime(2026, 9, 14, 4, 30, tzinfo=JST)
    assert wait_until_notify_time(now) == 2.5 * 3600
    assert sleeps == [2.5 * 3600]

def test_wait_until_notify_time_after(monkeypatch, sleeps):
    monkeypatch.setenv("NOTIFY_AT_JST", "07:00")
    now = datetime(2026, 9, 14, 7, 45, tzinfo=JST)
    assert wait_until_notify_time(now) == 0.0
    assert sleeps == []

def test_wait_until_notify_time_invalid(monkeypatch, sleeps):
    monkeypatch.setenv("NOTIFY_AT_JST", "7時")
    now = datetime(2026, 9, 14, 4, 30, tzinfo=JST)
    assert wait_until_notify_time(now) == 0.0
    assert sleeps == []

class _FakeResp:
    status_code = 200
    def json(self):
        return {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "[]"}]}}]}

def _capture_gemini_payload(monkeypatch, model):
    captured = {}
    def fake_post(url, json=None, timeout=None):
        captured["payload"] = json
        return _FakeResp()
    monkeypatch.setattr(gather, "GEMINI_MODEL", model)
    monkeypatch.setattr(gather.requests, "post", fake_post)
    assert gather.call_gemini_rest("prompt", "dummy-key") == []
    return captured["payload"]["generationConfig"]

def test_call_gemini_rest_disables_thinking_for_flash(monkeypatch):
    config = _capture_gemini_payload(monkeypatch, "gemini-2.5-flash")
    assert config["thinkingConfig"] == {"thinkingBudget": 0}

def test_call_gemini_rest_keeps_thinking_for_non_flash(monkeypatch):
    config = _capture_gemini_payload(monkeypatch, "gemini-2.5-pro")
    assert "thinkingConfig" not in config

def _write_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")

def test_load_today_digest(monkeypatch, tmp_path):
    digest_file = tmp_path / "digest.json"
    monkeypatch.setattr(gather, "DIGEST_FILE", str(digest_file))
    assert gather.load_today_digest() is None  # ファイルなし

    _write_json(digest_file, {"generated_at": "2020-01-05T07:00:00+09:00"})
    assert gather.load_today_digest() is None  # 古いダイジェスト

    today = datetime.now(JST).isoformat()
    _write_json(digest_file, {"generated_at": today, "overview": "x"})
    assert gather.load_today_digest()["overview"] == "x"

def test_notify_from_saved(monkeypatch, tmp_path, sleeps):
    data_file = tmp_path / "data.json"
    digest_file = tmp_path / "digest.json"
    _write_json(data_file, {"generated_at": "2026-09-14T04:20:00+09:00",
                            "articles": [{"title": "A", "score": 90}]})
    _write_json(digest_file, {"generated_at": datetime.now(JST).isoformat()})
    monkeypatch.setattr(gather, "DATA_FILE", str(data_file))
    monkeypatch.setattr(gather, "DIGEST_FILE", str(digest_file))
    monkeypatch.delenv("NOTIFY_AT_JST", raising=False)

    sent = {}
    monkeypatch.setattr(gather, "send_notifications", lambda arts: sent.setdefault("articles", arts))
    monkeypatch.setattr(gather, "send_digest_notification", lambda d: sent.setdefault("digest", d))

    gather.notify_from_saved()
    assert sent["articles"] == [{"title": "A", "score": 90}]
    assert "digest" in sent
    assert sleeps == []
