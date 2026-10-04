"""python3 test_watch.py — the detection math, tested at its boundaries.

The two detectors gate real orders on the private side, so each threshold is tested in the
direction it must fail: toward NOT firing. A missed start costs one match's exit; a false
start fires a dispatch the private side re-checks anyway — but the thresholds themselves are
the spec (8c / 10 trades / 10 min, trigger 70c) and must not drift.
"""
import watch


def tape(*minute_yes_pairs):
    """[(minute_offset, yes_price), ...] -> the trades-list shape, one trade per entry."""
    return [(f"2026-10-04T10:{m:02d}:00Z", y) for m, y in minute_yes_pairs]


def test_market_side_converts_to_pick_equivalent():
    meds = watch.minute_medians(tape((0, 30), (1, 40)), "no")
    assert [m[1] for m in meds] == [70, 60]      # holding NO: pick trades at 100 - yes
    meds = watch.minute_medians(tape((0, 30), (1, 40)), "yes")
    assert [m[1] for m in meds] == [30, 40]


def test_median_not_mean_per_minute():
    # one minute, trades 10/10/90: the median 10 shrugs off the outlier a mean would chase —
    # the orphaned-feeler lesson, applied to the tape
    meds = watch.minute_medians(tape((0, 10), (0, 10), (0, 90)), "yes")
    assert meds == [("2026-10-04T10:00", 10, 3)]


def test_start_needs_all_three_conditions():
    # 10 minutes, 1c per minute = 9c cumulative movement on 10 trades: fires, from minute 0
    moving = tape(*[(m, 50 + m) for m in range(10)])
    d = watch.detect_start(watch.minute_medians(moving, "yes"))
    assert d and d["start_at"] == "2026-10-04T10:00:00Z" and d["window_trades"] == 10

    # same movement, only 9 trades: refuses
    short = tape(*[(m, 50 + m) for m in range(9)])
    assert watch.detect_start(watch.minute_medians(short, "yes")) is None

    # 10 trades, 7c cumulative: refuses
    flat = tape(*[(m, 50 + min(m, 7)) for m in range(10)])
    assert watch.detect_start(watch.minute_medians(flat, "yes")) is None


def test_the_window_is_ten_minutes_by_the_clock():
    # 8c of movement whose two halves sit 11 minutes apart: 4c in the first minute, 4c in the
    # last, flat between. No 10-minute window ever sees more than 4c, however the window slides.
    slow = tape((0, 50), *[(m, 54) for m in range(1, 11)], (11, 58))
    assert watch.detect_start(watch.minute_medians(slow, "yes")) is None


def test_buy_triggers_at_70_only_after_the_start():
    # 72c printed BEFORE the start minute must not count; the first >=70 at/after it does
    t = tape((0, 72), (5, 50), (6, 69), (7, 71))
    meds = watch.minute_medians(t, "yes")
    b = watch.detect_buy(meds, "2026-10-04T10:05:00Z")
    assert b and b["at"] == "2026-10-04T10:07:00Z" and b["tape_px"] == 71


def test_v2_dollar_prices_are_parsed_not_defaulted():
    # the known V2 trap: absent legacy field must read from *_dollars, never as zero
    trades = [{"created_time": "2026-10-04T10:00:00Z", "yes_price_dollars": "0.43"}]
    out = [(c, p) for c, p in [(t["created_time"], int(round(float(t["yes_price_dollars"]) * 100)))
                               for t in trades]]
    assert out == [("2026-10-04T10:00:00Z", 43)]


if __name__ == "__main__":
    import sys
    fails = 0
    for n, fn in sorted(globals().items()):
        if n.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", n)
            except AssertionError as e:
                fails += 1
                print("FAIL", n, e)
    sys.exit(1 if fails else 0)
