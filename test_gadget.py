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
    data = gadget.load_gadgets(str(path))
    assert [it["url"] for it in pick_gadget_notifications(data)] == ["a"]
