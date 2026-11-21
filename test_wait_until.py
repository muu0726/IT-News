from datetime import datetime, timedelta

import wait_until
from wait_until import JST, parse_jst_slots, resolve_next_target, wait_until_jst


def test_parse_jst_slots():
    assert parse_jst_slots("") == []
    assert parse_jst_slots("   ,  ") == []
    assert parse_jst_slots("07:00") == [(7, 0)]
    assert parse_jst_slots(" 15:00 , 07:00 ") == [(7, 0), (15, 0)]
    assert parse_jst_slots("00:00,23:59") == [(0, 0), (23, 59)]
    assert parse_jst_slots("7時") is None
    assert parse_jst_slots("24:00") is None
    assert parse_jst_slots("07:60") is None
    assert parse_jst_slots("07:00:00") is None
    assert parse_jst_slots("07:00,bad") is None


def test_cross_day_wait_2237_to_0645():
    now = datetime(2026, 11, 20, 22, 37, tzinfo=JST)
    sleeps = []
    slept, done = wait_until_jst(
        "06:45",
        now=now,
        max_wait_sec=30600,
        sleep_fn=sleeps.append,
    )
    expected = 8 * 3600 + 8 * 60  # 29280秒
    assert slept == expected
    assert done is True
    assert sleeps == [expected]


def test_expired_target_does_not_wait_until_next_day():
    # 06:45 目標に対して 07:10 起動 -> 翌日 06:45 は 23h35m 後 (> max_wait 30600s) のため即時進行
    now = datetime(2026, 11, 21, 7, 10, tzinfo=JST)
    sleeps = []
    slept, done = wait_until_jst(
        "06:45",
        now=now,
        max_wait_sec=30600,
        sleep_fn=sleeps.append,
    )
    assert slept == 0.0
    assert done is True
    assert sleeps == []


def test_morning_slot_delay_into_afternoon_does_not_hit_1500():
    # 朝スロット単一指定 ("07:00") なら 14:37 に起動しても 15:00 と誤認せず即時進行
    now = datetime(2026, 11, 21, 14, 37, tzinfo=JST)
    sleeps = []
    slept, done = wait_until_jst(
        "07:00",
        now=now,
        max_wait_sec=30600,
        sleep_fn=sleeps.append,
    )
    assert slept == 0.0
    assert done is True
    assert sleeps == []


def test_two_stage_wait_handoff_and_single_stage_when_delayed():
    # 22:37 起動 (残り 29280s) -> wait_1 (上限 15000s) で途中終了 (done=False)
    t1 = datetime(2026, 11, 20, 22, 37, tzinfo=JST)
    sleeps_1 = []
    slept_1, done_1 = wait_until_jst(
        "06:45",
        now=t1,
        max_wait_sec=30600,
        max_sleep_sec=15000,
        sleep_fn=sleeps_1.append,
    )
    assert slept_1 == 15000.0
    assert done_1 is False
    assert sleeps_1 == [15000.0]

    # wait_2 は残り 14280s (<= 15600s) を待機して目標到達 (done=True)
    t2 = t1 + timedelta(seconds=slept_1)
    sleeps_2 = []
    slept_2, done_2 = wait_until_jst(
        "06:45",
        now=t2,
        max_wait_sec=30600,
        max_sleep_sec=15600,
        sleep_fn=sleeps_2.append,
    )
    assert slept_2 == 14280.0
    assert done_2 is True
    assert sleeps_2 == [14280.0]

    # 4時間半遅延して 03:07 起動 (残り 13080s <= 15000s) -> wait_1 のみで done=True
    t_delayed = datetime(2026, 11, 21, 3, 7, tzinfo=JST)
    sleeps_d = []
    slept_d, done_d = wait_until_jst(
        "06:45",
        now=t_delayed,
        max_wait_sec=30600,
        max_sleep_sec=15000,
        sleep_fn=sleeps_d.append,
    )
    assert slept_d == 13080.0
    assert done_d is True
    assert sleeps_d == [13080.0]


def test_chunked_wait_loop_heartbeat_and_drift_correction():
    # 仮想壁時計: 初回 sleep がわずかに長く進んでも次回残り時間で補正されること
    clock_state = {"now": datetime(2026, 11, 21, 6, 35, 0, tzinfo=JST)}
    sleeps = []

    def fake_now():
        return clock_state["now"]

    def fake_sleep(sec):
        sleeps.append(sec)
        # 1回目は +5秒ドリフトして進み、残りを壁時計で再計算できることを検証
        drift = 5 if len(sleeps) == 1 else 0
        clock_state["now"] += timedelta(seconds=sec + drift)

    slept, done = wait_until_jst(
        "07:00",
        max_wait_sec=3600,
        heartbeat_sec=600,
        sleep_fn=fake_sleep,
        now_fn=fake_now,
    )
    # 06:35 -> 07:00 (1500s):
    # 1回目: 600s sleep (時計は 605s 進み 06:45:05)
    # 2回目: 600s sleep (06:55:05)
    # 3回目: 295s sleep (07:00:00)
    assert sleeps == [600.0, 600.0, 295.0]
    assert slept == 1495.0
    assert done is True
    assert clock_state["now"] == datetime(2026, 11, 21, 7, 0, 0, tzinfo=JST)


def test_chunked_wait_loop_breaks_if_clock_stuck():
    # sleep しても時計が進まないモック環境で無限ループしないこと
    fixed = datetime(2026, 11, 21, 6, 50, 0, tzinfo=JST)
    sleeps = []
    slept, done = wait_until_jst(
        "07:00",
        max_wait_sec=3600,
        heartbeat_sec=300,
        sleep_fn=sleeps.append,
        now_fn=lambda: fixed,
    )
    assert sleeps == [300.0]
    assert slept == 300.0
    assert done is True


def test_cli_github_output(monkeypatch, tmp_path):
    gh_out = tmp_path / "github_output.txt"
    monkeypatch.setattr(
        wait_until,
        "wait_until_jst",
        lambda *a, **kw: (15000.0, False),
    )
    rc = wait_until.main(
        [
            "--target",
            "06:45",
            "--max-wait",
            "30600",
            "--max-sleep",
            "15000",
            "--github-output",
            str(gh_out),
        ]
    )
    assert rc == 0
    content = gh_out.read_text(encoding="utf-8")
    assert "done=false\n" in content
    assert "slept_sec=15000\n" in content


def test_resolve_next_target_without_next_day():
    now = datetime(2026, 11, 21, 15, 1, tzinfo=JST)
    assert resolve_next_target([(7, 0), (15, 0)], now, allow_next_day=False) is None
    assert resolve_next_target([(7, 0), (15, 0)], now, allow_next_day=True) == datetime(
        2026, 11, 22, 7, 0, tzinfo=JST
    )
