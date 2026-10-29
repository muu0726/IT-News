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

def test_wait_until_notify_time_multiple_slots(monkeypatch, sleeps):
    monkeypatch.setenv("NOTIFY_AT_JST", "07:00,15:00")
    # 04:30 -> 07:00 まで 2.5時間待機
    now_morning = datetime(2026, 9, 14, 4, 30, tzinfo=JST)
    assert wait_until_notify_time(now_morning) == 2.5 * 3600
    # 12:30 -> 07:00 は過ぎているので 15:00 まで 2.5時間待機
    now_afternoon = datetime(2026, 9, 14, 12, 30, tzinfo=JST)
    assert wait_until_notify_time(now_afternoon) == 2.5 * 3600
    # 15:30 -> 両方過ぎているので即時 (0秒)
    now_evening = datetime(2026, 9, 14, 15, 30, tzinfo=JST)
    assert wait_until_notify_time(now_evening) == 0.0
    assert sleeps == [2.5 * 3600, 2.5 * 3600]

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


def test_normalize_url_keeps_id_params_and_strips_utm():
    u1 = "https://example.com/post/?utm_source=rss&utm_medium=feed"
    assert gather.normalize_url(u1) == "https://example.com/post"
    hn1 = "https://news.ycombinator.com/item?id=100&utm_source=x"
    hn2 = "https://news.ycombinator.com/item?id=200"
    assert gather.normalize_url(hn1) == "https://news.ycombinator.com/item?id=100"
    assert gather.normalize_url(hn1) != gather.normalize_url(hn2)


def test_extract_main_text_prefers_article_block():
    sidebar = "<div class='sidebar'><p>" + ("ノイズ " * 120) + "</p></div>"
    main = "<article><p>" + ("本文テキストです。" * 40) + "</p></article>"
    text = gather.extract_main_text(f"<html><body>{sidebar}{main}</body></html>")
    assert "本文テキストです" in text
    assert "ノイズ" not in text


def test_extract_main_text_falls_back_to_ogp_and_json_ld():
    html_doc = """
    <html>
    <head>
      <meta property="og:description" content="OGPの概要説明テキストです。">
      <script type="application/ld+json">
        {"@context": "https://schema.org", "articleBody": "JSON-LD構造化データの本文です。"}
      </script>
    </head>
    <body><div id="app">Loading...</div></body>
    </html>
    """
    text = gather.extract_main_text(html_doc)
    assert "OGPの概要説明テキストです" in text
    assert "JSON-LD構造化データの本文です" in text


def test_call_gemini_rest_passes_response_schema(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["payload"] = json
        return _FakeResp()

    monkeypatch.setattr(gather.requests, "post", fake_post)
    gather.call_gemini_rest("prompt", "dummy-key", response_schema=gather.BATCH_ANALYSIS_SCHEMA)
    config = captured["payload"]["generationConfig"]
    assert config["responseSchema"] == gather.BATCH_ANALYSIS_SCHEMA


def test_reuse_and_merge_today_articles():
    run1_ts = "2026-10-29T07:00:00+09:00"
    run2_ts = "2026-10-29T15:00:00+09:00"
    existing_today = [
        {
            "title": "Article A",
            "url": "https://example.com/a",
            "score": 85,
            "summary": "a\nb\nc",
            "score_reason": "Good",
            "tags": ["AI"],
            "category": "AI・機械学習",
            "analysis_status": "ok",
            "new_at": run1_ts,
        },
        {
            "title": "Article B (Morning only)",
            "url": "https://example.com/b",
            "score": 75,
            "summary": "x\ny\nz",
            "analysis_status": "ok",
            "new_at": run1_ts,
        },
    ]
    fetched = [
        {"title": "Article A", "url": "https://example.com/a?utm_source=rss", "is_hot": True},
        {"title": "Article C (Afternoon new)", "url": "https://example.com/c", "is_hot": False},
    ]
    reused, to_analyze = gather.reuse_analyzed_articles(fetched, existing_today)
    assert len(reused) == 1
    assert len(to_analyze) == 1
    assert reused[0]["score"] == 85
    assert reused[0]["new_at"] == run1_ts
    assert to_analyze[0]["title"] == "Article C (Afternoon new)"

    # Simulate analyzing Article C
    to_analyze[0].update({"score": 92, "analysis_status": "ok"})
    merged = gather.merge_today_articles(existing_today, reused + to_analyze, run2_ts)
    assert len(merged) == 3
    by_title = {m["title"]: m for m in merged}
    assert by_title["Article A"]["new_at"] == run1_ts
    assert by_title["Article B (Morning only)"]["new_at"] == run1_ts
    assert by_title["Article C (Afternoon new)"]["new_at"] == run2_ts


def test_notify_from_saved_filters_only_latest_run(monkeypatch, tmp_path, sleeps):
    data_file = tmp_path / "data.json"
    digest_file = tmp_path / "digest.json"
    _write_json(
        data_file,
        {
            "generated_at": "2026-10-29T15:00:00+09:00",
            "articles": [
                {"title": "Morning Art", "score": 90, "new_at": "2026-10-29T07:00:00+09:00"},
                {"title": "Afternoon Art", "score": 88, "new_at": "2026-10-29T15:00:00+09:00"},
            ],
        },
    )
    monkeypatch.setattr(gather, "DATA_FILE", str(data_file))
    monkeypatch.setattr(gather, "DIGEST_FILE", str(digest_file))
    monkeypatch.delenv("NOTIFY_AT_JST", raising=False)

    sent = {}
    monkeypatch.setattr(gather, "send_notifications", lambda arts: sent.setdefault("articles", arts))
    gather.notify_from_saved()
    assert len(sent["articles"]) == 1
    assert sent["articles"][0]["title"] == "Afternoon Art"


def test_build_search_index(tmp_path):
    arch_dir = tmp_path / "archive"
    arch_dir.mkdir()
    _write_json(
        arch_dir / "2026-10-28.json",
        {"articles": [{"title": "Old", "url": "https://example.com/1", "score": 70, "body": "heavy"}]},
    )
    _write_json(
        arch_dir / "2026-10-29.json",
        {
            "articles": [
                {"title": "Updated", "url": "https://example.com/1", "score": 80, "body": "heavy"},
                {"title": "New", "url": "https://example.com/2", "score": 90},
            ]
        },
    )
    idx_path = arch_dir / "search_index.json"

    payload = gather.build_search_index(str(arch_dir), str(idx_path))
    assert payload["total_count"] == 2
    assert payload["days_count"] == 2
    assert payload["articles"][0]["title"] == "Updated"
    assert "body" not in payload["articles"][0]
    assert idx_path.exists()


def test_estimate_reading_minutes_ja_and_en(monkeypatch):
    # 日本語: 1800文字 -> 約3分
    ja_text = "あ" * 1800
    assert gather.estimate_reading_minutes(ja_text) == 3
    # 英語: 660単語 -> 約3分
    en_text = " ".join(["architecture"] * 660)
    assert gather.estimate_reading_minutes(en_text) == 3
    # 短文は最小1分
    assert gather.estimate_reading_minutes("短いテキスト") == 1

    class _HtmlResp:
        ok = True
        headers = {"Content-Type": "text/html; charset=utf-8"}
        text = "<html><body><article><p>" + ("技術記事の本文テキストです。" * 200) + "</p></article></body></html>"

    monkeypatch.setattr(gather.requests, "get", lambda *a, **kw: _HtmlResp())
    art = {"url": "https://example.com/long-article"}
    gather._fetch_one_body(art)
    assert len(art["_body"]) <= gather.BODY_MAX_CHARS
    assert art["reading_min"] >= 4


def test_fetch_hatena_counts_batches_and_normalizes(monkeypatch):
    calls = []

    class _HatenaResp:
        status_code = 200

        def __init__(self, params):
            self._urls = [u for k, u in params if k == "url"]

        def json(self):
            return {u: idx + 10 for idx, u in enumerate(self._urls)}

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(params)
        return _HatenaResp(params)

    monkeypatch.setattr(gather.requests, "get", fake_get)
    articles = [{"url": f"https://example.com/item-{i}"} for i in range(55)]
    gather.fetch_hatena_counts(articles)
    # 55件 -> 50件 + 5件 の2バッチに分割されること
    assert len(calls) == 2
    assert articles[0]["hatena_count"] == 10
    assert articles[54]["hatena_count"] == 14


def test_load_recent_past_articles_and_prev_seen_suppression(monkeypatch, tmp_path, sleeps):
    arch_dir = tmp_path / "archive"
    arch_dir.mkdir()
    monkeypatch.setattr(gather, "ARCHIVE_DIR", str(arch_dir))

    # 2026-10-28 (前日) のアーカイブ
    _write_json(
        arch_dir / "2026-10-28.json",
        {
            "generated_at": "2026-10-28T15:00:00+09:00",
            "articles": [
                {
                    "title": "Yesterday Hit",
                    "url": "https://example.com/yesterday",
                    "summary": "s1\ns2\ns3",
                    "tags": ["Rust"],
                    "category": "プログラミング言語",
                    "score": 92,
                    "score_reason": "Great",
                    "analysis_status": "ok",
                    "new_at": "2026-10-28T15:00:00+09:00",
                }
            ],
        },
    )

    now = datetime(2026, 10, 29, 7, 0, tzinfo=JST)
    run_at = now.isoformat()
    past_map = gather.load_recent_past_articles(now, days=3)
    assert "https://example.com/yesterday" in past_map

    fetched = [
        {"title": "Yesterday Hit raw", "url": "https://example.com/yesterday?utm_source=hn"},
        {"title": "Brand New Today", "url": "https://example.com/today-new"},
    ]
    reused, to_analyze = gather.reuse_analyzed_articles(fetched, [], past_map)
    assert len(reused) == 1
    assert reused[0]["is_prev_seen"] is True
    assert reused[0]["new_at"] == "2026-10-28T15:00:00+09:00"
    assert len(to_analyze) == 1

    to_analyze[0].update({"score": 88, "analysis_status": "ok", "new_at": run_at})
    merged = gather.merge_today_articles([], reused + to_analyze, run_at)

    # 保存して notify_from_saved が既出記事を通知しないことを確認
    data_file = tmp_path / "data.json"
    digest_file = tmp_path / "digest.json"
    _write_json(data_file, {"generated_at": run_at, "articles": merged})
    monkeypatch.setattr(gather, "DATA_FILE", str(data_file))
    monkeypatch.setattr(gather, "DIGEST_FILE", str(digest_file))
    monkeypatch.delenv("NOTIFY_AT_JST", raising=False)

    sent = {}
    monkeypatch.setattr(gather, "send_notifications", lambda arts: sent.setdefault("articles", arts))
    gather.notify_from_saved()
    assert [a["title"] for a in sent["articles"]] == ["Brand New Today"]


def test_generate_daily_briefing_ai_and_fallback(monkeypatch):
    articles = [
        {"title": "LLM高速化", "source": "Zenn", "score": 90, "tags": ["LLM", "AI"], "category": "AI・機械学習", "analysis_status": "ok"},
        {"title": "Rust 2.0動向", "source": "Publickey", "score": 85, "tags": ["Rust", "AI"], "category": "プログラミング言語", "analysis_status": "ok"},
        {"title": "ゼロトラスト設計", "source": "Qiita", "score": 80, "tags": ["Security"], "category": "セキュリティ", "analysis_status": "ok"},
    ]

    # 1. APIキー未設定時のフォールバック検証（朝刊）
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    bf_fallback = gather.generate_daily_briefing(articles, run_at="2026-10-29T06:30:00+09:00")
    assert bf_fallback["edition"] == "朝刊"
    assert len(bf_fallback["summary_lines"]) == 3
    assert bf_fallback["focus_tags"][0] == "AI"

    # 2. Gemini API 成功時の検証（夕刊 + 絵文字除去）
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-key")
    monkeypatch.setattr(
        gather,
        "call_gemini_rest",
        lambda prompt, key, response_schema=None: {
            "edition": "夕刊",
            "summary_lines": ["🚀 LLM推論最適化が急進", "Rust採用事例が拡大", "ゼロトラスト運用が標準化"],
            "focus_tags": ["🔥LLM", "Rust"],
        },
    )
    bf_ai = gather.generate_daily_briefing(articles, run_at="2026-10-29T14:30:00+09:00")
    assert bf_ai["edition"] == "夕刊"
    assert bf_ai["summary_lines"][0] == "LLM推論最適化が急進"
    assert bf_ai["focus_tags"][0] == "LLM"


def test_fetch_hackernews_preserves_story_order(monkeypatch):
    class _TopResp:
        def json(self):
            return [101, 102, 103]

    class _ItemResp:
        def __init__(self, sid):
            self._sid = sid

        def json(self):
            return {"title": f"HN-{self._sid}", "url": f"https://example.com/{self._sid}", "time": 1700000000, "score": self._sid, "descendants": 5}

    def fake_safe_get(url, timeout=15):
        if url == gather.HN_TOP_URL:
            return _TopResp()
        sid = int(url.split("/")[-1].replace(".json", ""))
        return _ItemResp(sid)

    monkeypatch.setattr(gather, "safe_get", fake_safe_get)
    res = gather.fetch_hackernews()
    assert [a["title"] for a in res] == ["HN-101", "HN-102", "HN-103"]



