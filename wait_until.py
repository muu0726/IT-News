#!/usr/bin/env python3
"""JST 指定時刻までの待機モジュール（標準ライブラリのみ）。

GitHub Actions の schedule 遅延を吸収するため、収集ジョブ前の事前待機（2段階分割対応）
および通知ジョブの定時待機の両方で共通利用する。
"""

from __future__ import annotations

import argparse
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

JST = timezone(timedelta(hours=9))
DEFAULT_MAX_WAIT_SEC = 3600.0
DEFAULT_HEARTBEAT_SEC = 600.0


def parse_jst_slots(target_str: str) -> list[tuple[int, int]] | None:
    """カンマ区切り HH:MM 文字列を [(hour, minute), ...] にパースする。

    空文字列は []、形式不正または範囲外が含まれる場合は None を返す。
    """
    slots: list[tuple[int, int]] = []
    for raw in (target_str or "").split(","):
        part = raw.strip()
        if not part:
            continue
        pieces = part.split(":")
        if len(pieces) != 2:
            return None
        try:
            hour = int(pieces[0])
            minute = int(pieces[1])
        except ValueError:
            return None
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        slots.append((hour, minute))
    return sorted(slots)


def resolve_next_target(
    slots: list[tuple[int, int]],
    now: datetime,
    allow_next_day: bool = True,
) -> datetime | None:
    """現在時刻より後に到来する最も近い目標時刻 (datetime) を返す。"""
    if not slots:
        return None
    day_offsets = (0, 1) if allow_next_day else (0,)
    candidates: list[datetime] = []
    for day_offset in day_offsets:
        base = now + timedelta(days=day_offset)
        for hour, minute in sorted(slots):
            dt = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if (dt - now).total_seconds() > 0:
                candidates.append(dt)
    return min(candidates) if candidates else None


def wait_until_jst(
    target_str: str,
    now: datetime | None = None,
    max_wait_sec: float = DEFAULT_MAX_WAIT_SEC,
    max_sleep_sec: float | None = None,
    heartbeat_sec: float = DEFAULT_HEARTBEAT_SEC,
    sleep_fn: Callable[[float], None] | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> tuple[float, bool]:
    """指定された JST 時刻まで待機し、(実待機秒数, 完了フラグ) を返す。

    done=True は目標時刻へ到達（または未指定・無効・上限超過により即時進行）を示し、
    done=False は max_sleep_sec 上限により途中終了し次ステージへ引き継ぐことを示す。
    """
    cleaned = (target_str or "").strip()
    if not cleaned:
        return 0.0, True

    slots = parse_jst_slots(cleaned)
    if slots is None:
        print(
            f"[WARN] Invalid NOTIFY_AT_JST: {target_str!r} -- notifying now",
            flush=True,
        )
        return 0.0, True
    if not slots:
        return 0.0, True

    fixed_clock = now is not None
    clock = now_fn or (lambda: datetime.now(JST))
    start_now = now if fixed_clock else clock()

    target = resolve_next_target(slots, start_now, allow_next_day=True)
    if target is None:
        return 0.0, True

    wait_sec = (target - start_now).total_seconds()
    if wait_sec > max_wait_sec:
        print(
            f"[INFO] Target time {target.strftime('%Y-%m-%d %H:%M JST')} is "
            f"{wait_sec:.0f}s away (> max {max_wait_sec:.0f}s) "
            f"-- likely delayed or out of schedule window. Proceeding immediately.",
            flush=True,
        )
        return 0.0, True

    if max_sleep_sec is not None and max_sleep_sec > 0 and wait_sec > max_sleep_sec:
        planned_sleep = float(max_sleep_sec)
        will_reach_target = False
    else:
        planned_sleep = wait_sec
        will_reach_target = True

    sleeper = sleep_fn or time.sleep

    if fixed_clock:
        print(
            f"[INFO] Waiting {planned_sleep:.0f}s until {target.isoformat()} to notify",
            flush=True,
        )
        sleeper(planned_sleep)
        return planned_sleep, will_reach_target

    stage_deadline = (
        target if will_reach_target else (start_now + timedelta(seconds=planned_sleep))
    )
    print(
        f"[INFO] Waiting {planned_sleep:.0f}s "
        f"(target: {target.isoformat()}, deadline: {stage_deadline.isoformat()}, "
        f"will_reach_target: {will_reach_target})",
        flush=True,
    )

    total_slept = 0.0
    while True:
        cur = clock()
        rem = (stage_deadline - cur).total_seconds()
        if rem <= 0:
            break
        step = min(rem, heartbeat_sec) if heartbeat_sec > 0 else rem
        if heartbeat_sec > 0 and rem > heartbeat_sec:
            print(
                f"[INFO] Wait heartbeat: {rem:.0f}s remaining until "
                f"{stage_deadline.strftime('%Y-%m-%d %H:%M:%S JST')} "
                f"(now: {cur.strftime('%H:%M:%S JST')})",
                flush=True,
            )
        sleeper(step)
        total_slept += step
        after_rem = (stage_deadline - clock()).total_seconds()
        # モック sleep 等で壁時計が進まない場合の無限ループ防止
        if step >= 1.0 and (rem - after_rem) < step * 0.25:
            print(
                "[WARN] Clock did not advance during sleep -- breaking wait loop",
                flush=True,
            )
            break
        # 極端な時計巻き戻し対策
        if total_slept >= planned_sleep + 120:
            print(
                "[WARN] Wait loop exceeded planned budget + 120s -- breaking",
                flush=True,
            )
            break

    final_now = clock()
    done = will_reach_target or (target - final_now).total_seconds() <= 0
    print(
        f"[INFO] Wait finished at {final_now.strftime('%Y-%m-%d %H:%M:%S JST')} "
        f"(slept {total_slept:.0f}s, done={done})",
        flush=True,
    )
    return total_slept, done


def _resolve_max_wait(arg_value: float | None) -> float:
    if arg_value is not None:
        return arg_value
    raw = os.environ.get("NOTIFY_MAX_WAIT_SEC", str(DEFAULT_MAX_WAIT_SEC))
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_MAX_WAIT_SEC


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Wait until target JST time(s)")
    parser.add_argument(
        "--target",
        default=os.environ.get("NOTIFY_AT_JST", ""),
        help="目標時刻 (HH:MM または カンマ区切り HH:MM,HH:MM)",
    )
    parser.add_argument(
        "--max-wait",
        type=float,
        default=None,
        help="待機を許可する最大秒数（超過時は待たずに即時進行）",
    )
    parser.add_argument(
        "--max-sleep",
        type=float,
        default=None,
        help="このステージで待機する上限秒数（分割待機用）",
    )
    parser.add_argument(
        "--heartbeat",
        type=float,
        default=DEFAULT_HEARTBEAT_SEC,
        help="チャンク待機・進捗出力間隔（秒）",
    )
    parser.add_argument(
        "--github-output",
        default=None,
        help="GitHub Actions の $GITHUB_OUTPUT ファイルパス",
    )
    args = parser.parse_args(argv)

    max_wait = _resolve_max_wait(args.max_wait)
    slept_sec, done = wait_until_jst(
        args.target,
        max_wait_sec=max_wait,
        max_sleep_sec=args.max_sleep,
        heartbeat_sec=args.heartbeat,
    )

    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as f:
            f.write(f"done={'true' if done else 'false'}\n")
            f.write(f"slept_sec={int(round(slept_sec))}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
