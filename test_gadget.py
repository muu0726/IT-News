from datetime import datetime, timedelta, timezone

import gadget
from gadget import (
    JST,
    apply_gadget_analysis,
    is_recent,
    merge_gadgets,
    pick_gadget_notifications,
    select_new_articles,
    validate_gadget_item,
)

RUN_AT = "2026-09-17T04:20:00+09:00"
PREV_RUN = "2026-09-16T04:20:00+09:00"

def _item(url, score=80, new_at=RUN_AT, is_new=True, product=None, status="ok"):
    return {
        "url": url, "title": f"title {url}", "product_name": product or url,
        "score": score, "new_at": new_at, "is_new_product": is_new, "analysis_status": status,
    }

# ── 通知対象の選定 ──
def test_pick_only_current_run_new_products():
    data = {"generated_at": RUN_AT, "items": [
        _item("a", score=90),
        _item("b", score=95, new_at=PREV_RUN),     # 前回の新着
        _item("c", score=95, is_new=False),        # レビュー等
        _item("d", score=59),                      # 閾値未満
        _item("e", score=95, status="error"),      # 解析失敗
    ]}
    assert [it["url"] for it in pick_gadget_notifications(data)] == ["a"]

def test_pick_max_three_sorted_by_score():
    data = {"generated_at": RUN_AT, "items": [_item(u, score=s) for u, s in
                                               [("a", 61), ("b", 99), ("c", 70), ("d", 85), ("e", 75)]]}
    assert [it["url"] for it in pick_gadget_notifications(data)] == ["b", "d", "e"]

def test_pick_dedupes_same_product():
    data = {"generated_at": RUN_AT, "items": [
        _item("a", score=90, product="iPhone 18 Pro"),
        _item("b", score=85, product="iphone18 pro"),
        _item("c", score=70, product="Pixel 11"),
    ]}
    assert [it["url"] for it in pick_gadget_notifications(data)] == ["a", "c"]

def test_pick_none():
    assert pick_gadget_notifications({"generated_at": RUN_AT, "items": []}) == []
    assert pick_gadget_notifications({"items": [_item("a")]}) == []

# ── 新着抽出・マージ ──
def test_select_new_articles_skips_analyzed_but_retries_failed():
    existing = [
        {"url": "https://example.com/ok/", "analysis_status": "ok"},
        {"url": "https://example.com/failed", "analysis_status": "error"},
    ]
    fetched = [{"url": "https://EXAMPLE.com/ok"}, {"url": "https://example.com/failed"},
               {"url": "https://example.com/new"}]
    urls = [a["url"] for a in select_new_articles(fetched, existing)]
    assert urls == ["https://example.com/failed", "https://example.com/new"]

def test_merge_keeps_first_seen_and_prunes_old():
    now = datetime(2026, 9, 17, 4, 20, tzinfo=JST)
    old_seen = (now - timedelta(days=31)).isoformat()
    existing = [
        {"url": "https://x.com/old", "first_seen": old_seen, "published": "2026-08-16T00:00:00+00:00"},
        {"url": "https://x.com/retry", "first_seen": PREV_RUN, "analysis_status": "error",
         "published": "2026-09-16T00:00:00+00:00"},
    ]
    analyzed = [
        {"url": "https://x.com/retry", "analysis_status": "ok", "_body": "x",
         "published": "2026-09-16T00:00:00+00:00"},
        {"url": "https://x.com/new", "analysis_status": "ok", "published": "2026-09-16T12:00:00+00:00"},
    ]
    items = merge_gadgets(existing, analyzed, RUN_AT, now)
    by_url = {it["url"]: it for it in items}
    assert "https://x.com/old" not in by_url
    assert by_url["https://x.com/retry"]["first_seen"] == PREV_RUN
    assert by_url["https://x.com/retry"]["analysis_status"] == "ok"
    assert "_body" not in by_url["https://x.com/retry"]
    assert by_url["https://x.com/new"]["first_seen"] == RUN_AT
    assert [it["url"] for it in items] == ["https://x.com/new", "https://x.com/retry"]

# ── 収集の期間フィルタ ──
def test_is_recent():
    now = datetime(2026, 9, 17, 0, 0, tzinfo=timezone.utc)
    assert is_recent("2026-09-16T01:00:00+00:00", now)
    assert not is_recent("2026-09-14T23:00:00+00:00", now)
    assert is_recent("not a date", now)

# ── Gemini 応答の検証・反映 ──
def test_validate_gadget_item():
    ok = {"id": 0, "title": "t", "summary": "s", "is_new_product": True, "score": 70}
    assert validate_gadget_item(ok)
    assert not validate_gadget_item({**ok, "id": "0"})
    assert not validate_gadget_item({**ok, "score": "high"})
    assert not validate_gadget_item({k: v for k, v in ok.items() if k != "is_new_product"})
    assert not validate_gadget_item("x")

def test_apply_gadget_analysis_normalizes_fields():
    art = {"title": "orig", "url": "u"}
    apply_gadget_analysis(art, {
        "id": 0, "title": "新型スマホ発表", "summary": "s", "product_name": "null",
        "brand": " Sony ", "category": "未知のカテゴリ", "is_new_product": "true",
        "price": "", "release_date": None, "score": 150, "score_reason": "r",
    }, RUN_AT)
    assert art["title"] == "新型スマホ発表"
    assert art["product_name"] is None
    assert art["brand"] == "Sony"
    assert art["category"] == "その他"
    assert art["is_new_product"] is True
    assert art["price"] is None
    assert art["score"] == 100
    assert art["analysis_status"] == "ok"
    assert art["new_at"] == RUN_AT

def test_stale_file_is_not_notified(monkeypatch):
    sent = []
    monkeypatch.setattr(gadget, "send_gadget_discord", lambda items: sent.append(items))
    monkeypatch.setattr(gadget, "send_gadget_slack", lambda items: None)
    monkeypatch.setattr(gadget, "send_gadget_line", lambda items: None)

    stale = {"generated_at": PREV_RUN, "items": [_item("a", new_at=PREV_RUN)]}
    gadget.send_gadget_notifications(stale)
    assert sent == []

    today = datetime.now(JST).isoformat()
    fresh = {"generated_at": today, "items": [_item("a", new_at=today)]}
    gadget.send_gadget_notifications(fresh)
    assert [it["url"] for it in sent[0]] == ["a"]

def test_notify_only_uses_saved_file(monkeypatch, tmp_path):
    path = tmp_path / "gadgets.json"
    gadget.save_gadgets([_item("a", score=90)], RUN_AT, path=str(path))
    raw = path.read_text(encoding="utf-8")
    assert "\n" not in raw  # コンパクト JSON 出力
    data = gadget.load_gadgets(str(path))
    assert [it["url"] for it in pick_gadget_notifications(data)] == ["a"]


def test_generate_gadget_rss_feed(tmp_path):
    feed_path = tmp_path / "gadget_feed.xml"
    items = [
        {
            "url": "https://example.com/p1?a=1&b=2",
            "title": "ソニーが新型ヘッドホンを発表",
            "product_name": "WH-1000XM7",
            "brand": "Sony & Co.",
            "category": "オーディオ",
            "is_new_product": True,
            "price": "5万9400円",
            "release_date": "2026年11月10日",
            "score": 92,
            "summary": "第1行\n第2行",
            "source": "AV Watch",
            "published": "2026-10-29T03:00:00+00:00",
            "analysis_status": "ok",
        },
        {
            "url": "https://example.com/review",
            "title": "長期レビュー記事",
            "product_name": "Old Phone",
            "is_new_product": False,
            "score": 95,
            "analysis_status": "ok",
        },
    ]
    gadget.generate_gadget_rss_feed(items, path=str(feed_path))
    xml_text = feed_path.read_text(encoding="utf-8")
    assert "<rss version=\"2.0\">" in xml_text
    assert "WH-1000XM7" in xml_text
    assert "Sony &amp; Co." in xml_text
    assert "https://example.com/p1?a=1&amp;b=2" in xml_text
    assert "5万9400円" in xml_text
    assert "長期レビュー記事" not in xml_text


def test_fetch_gadget_feeds_parallel_preserves_order(monkeypatch):
    now = datetime(2026, 10, 29, 6, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        gadget,
        "GADGET_FEEDS",
        {"FeedA": "https://a.example/rss", "FeedB": "https://b.example/rss"},
    )

    class _FakeResp:
        def __init__(self, content):
            self.content = content

    calls = []

    def fake_safe_get(url, timeout=15):
        calls.append((url, timeout))
        if "a.example" in url:
            xml = (
                b'<?xml version="1.0" encoding="UTF-8"?>'
                b'<rss version="2.0"><channel><item>'
                b'<title>Item A</title><link>https://a.example/1</link>'
                b'<pubDate>Thu, 29 Oct 2026 01:00:00 +0000</pubDate>'
                b'</item></channel></rss>'
            )
            return _FakeResp(xml)
        xml = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<rss version="2.0"><channel><item>'
            b'<title>Item B</title><link>https://b.example/1</link>'
            b'<pubDate>Thu, 29 Oct 2026 02:00:00 +0000</pubDate>'
            b'</item></channel></rss>'
        )
        return _FakeResp(xml)

    monkeypatch.setattr(gadget, "safe_get", fake_safe_get)
    items = gadget.fetch_gadget_feeds(now)
    assert [it["title"] for it in items] == ["Item A", "Item B"]
    assert all(timeout == 15 for _, timeout in calls)


def test_merge_prunes_old_errors_earlier_than_ok_items():
    now = datetime(2026, 11, 21, 12, 0, tzinfo=JST)
    existing = [
        {"url": "https://x.com/old-ok", "first_seen": (now - timedelta(days=10)).isoformat(), "analysis_status": "ok"},
        {"url": "https://x.com/old-err", "first_seen": (now - timedelta(days=4)).isoformat(), "analysis_status": "error"},
        {"url": "https://x.com/recent-err", "first_seen": (now - timedelta(days=1)).isoformat(), "analysis_status": "error"},
    ]
    items = gadget.merge_gadgets(existing, [], RUN_AT, now=now)
    urls = {it["url"] for it in items}
    assert "https://x.com/old-ok" in urls
    assert "https://x.com/old-err" not in urls
    assert "https://x.com/recent-err" in urls


def test_collect_retry_gadgets_requeues_recent_errors_with_cap():
    now = datetime(2026, 11, 21, 12, 0, tzinfo=JST)
    existing = [
        {"url": "https://x.com/ok", "first_seen": (now - timedelta(days=1)).isoformat(), "analysis_status": "ok", "published": "2026-11-20T10:00:00+00:00"},
        {"url": "https://x.com/err-old", "first_seen": (now - timedelta(days=5)).isoformat(), "analysis_status": "error", "published": "2026-11-16T10:00:00+00:00"},
        {"url": "https://x.com/err-1", "first_seen": (now - timedelta(days=1)).isoformat(), "analysis_status": "error", "published": "2026-11-20T11:00:00+00:00"},
        {"url": "https://x.com/skip-2", "first_seen": (now - timedelta(days=1)).isoformat(), "analysis_status": "skipped", "published": "2026-11-20T12:00:00+00:00"},
        {"url": "https://x.com/err-3", "first_seen": (now - timedelta(days=1)).isoformat(), "analysis_status": "error", "published": "2026-11-20T09:00:00+00:00"},
    ]
    already_queued = [{"url": "https://x.com/err-1?utm_source=rss"}]
    retries = gadget.collect_retry_gadgets(existing, already_queued, now=now, max_retry=1)
    assert [r["url"] for r in retries] == ["https://x.com/skip-2"]
    assert "analysis_status" not in retries[0]


def test_analyze_gadgets_handles_no_key_missing_item_and_timeout(monkeypatch):
    # 1. API キー未設定時は skipped
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    no_key = [{"title": "G0", "url": "https://x.com/g0"}]
    gadget.analyze_gadgets(no_key, RUN_AT)
    assert no_key[0]["analysis_status"] == "skipped"

    # 2. 部分成功 + バッチ欠落 error + タイムアウト skipped
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-key")
    monkeypatch.setattr(gadget, "ANALYSIS_BATCH_SIZE", 2)
    monkeypatch.setattr(gadget, "GADGET_TIMEOUT_SEC", 10)
    t_state = {"now": 50.0}
    monkeypatch.setattr(gadget.time, "monotonic", lambda: t_state["now"])
    monkeypatch.setattr(gadget.time, "sleep", lambda s: None)

    def fake_gemini(prompt, key, response_schema=None, deadline=None):
        assert deadline == 60.0
        t_state["now"] = 70.0  # 1バッチ目でタイムアウト超過
        return [{
            "id": 0,
            "title": "新型タブレット発売",
            "summary": "s1\ns2\ns3",
            "product_name": "Tab Pro",
            "brand": "Acme",
            "category": "タブレット",
            "is_new_product": True,
            "price": "9万8000円",
            "release_date": "2026年12月",
            "score": 88,
            "score_reason": "注目",
        }]

    monkeypatch.setattr(gadget, "call_gemini_rest", fake_gemini)
    items = [
        {"title": "raw0", "url": "https://x.com/0"},
        {"title": "raw1", "url": "https://x.com/1"},
        {"title": "raw2", "url": "https://x.com/2"},
    ]
    gadget.analyze_gadgets(items, RUN_AT)
    assert items[0]["analysis_status"] == "ok" and items[0]["new_at"] == RUN_AT
    assert items[1]["analysis_status"] == "error"
    assert items[2]["analysis_status"] == "skipped"


def test_send_gadget_channels_and_env_guards(monkeypatch):
    posted = []
    monkeypatch.setattr(
        gadget,
        "post_webhook",
        lambda url, payload, name, headers=None, timeout=15: posted.append((url, name, payload, headers)) or True,
    )
    sample = [{
        "url": "https://x.com/p1",
        "title": "新型カメラ発表",
        "product_name": "Cam X1",
        "brand": "BrandA",
        "category": "カメラ",
        "price": "15万円",
        "release_date": "2026年12月",
        "score": 90,
        "summary": "行1\n行2",
        "source": "AV Watch",
    }]

    # 環境変数未設定時は送信しない
    for env_key in ("DISCORD_WEBHOOK_URL", "SLACK_WEBHOOK_URL", "LINE_CHANNEL_ACCESS_TOKEN"):
        monkeypatch.delenv(env_key, raising=False)
    gadget.send_gadget_discord(sample)
    gadget.send_gadget_slack(sample)
    gadget.send_gadget_line(sample)
    assert posted == []

    # 設定時は3チャネルすべてへ整形済みペイロードを送信
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/hook")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://slack.example/hook")
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "line-tok")
    gadget.send_gadget_discord(sample)
    gadget.send_gadget_slack(sample)
    gadget.send_gadget_line(sample)
    assert len(posted) == 3
    assert posted[0][2]["embeds"][0]["title"] == "Cam X1"
    assert "blocks" in posted[1][2]
    assert posted[2][3] == {"Authorization": "Bearer line-tok"}



