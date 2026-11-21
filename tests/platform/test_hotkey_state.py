"""快捷键单一按住状态（按下区间 + 放行）规则测试：只喂区间与放行事件，不碰真实键盘或门户。"""
from __future__ import annotations

from _linux.hotkey_state import HotkeyPressState, ts_le


def _kinds(edges) -> list[str]:
    return [edge.kind for edge in edges]


def test_activation_inside_untriggered_interval_triggers_once() -> None:
    state = HotkeyPressState()
    assert state.interval_started("voice", 1000) == ()
    assert _kinds(state.activated("voice", 1000)) == ["voice_start"]
    assert state.voice_active is True

    state.interval_started("screenshot", 2000)
    assert [(e.kind, e.action) for e in state.activated("screenshot", 2000)] == [("fire", "screenshot")]

    state.interval_started("annotate", 3000)
    assert [(e.kind, e.action) for e in state.activated("annotate", 3000)] == [("fire", "annotate")]


def test_repeats_inside_triggered_interval_and_duplicate_reports_have_no_effect() -> None:
    state = HotkeyPressState()
    state.interval_started("voice", 1000)
    state.activated("voice", 1000)
    assert state.activated("voice", 1500) == ()
    assert state.activated("voice", 1530) == ()
    assert state.interval_started("voice", 1600) == ()  # 区间未结束时重复上报开始
    assert _kinds(state.interval_ended("voice", 2000)) == ["voice_stop"]
    assert state.interval_ended("voice", 2010) == ()  # 重复上报结束
    assert state.voice_active is False


def test_early_activation_waits_for_interval_and_is_dropped_when_no_interval_contains_it() -> None:
    state = HotkeyPressState()
    assert state.activated("ocr", 5000) == ()
    assert _kinds(state.interval_started("ocr", 5000)) == ["fire"]

    assert state.activated("ocr", 6000) == ()
    state.interval_ended("ocr", 5100)
    assert state.interval_started("ocr", 6500) == ()  # 6000 早于新区间开始：配不上，丢弃
    assert _kinds(state.activated("ocr", 6500)) == ["fire"]


def test_activation_after_its_interval_already_ended_starts_and_stops_at_once() -> None:
    state = HotkeyPressState()
    state.interval_started("voice", 1000)
    state.interval_ended("voice", 1040)
    assert _kinds(state.activated("voice", 1000)) == ["voice_start", "voice_stop"]
    assert state.voice_active is False

    state.interval_started("repeat", 2000)
    state.interval_ended("repeat", 2030)
    assert _kinds(state.activated("repeat", 2000)) == ["fire"]


def test_repeat_stamped_after_release_neither_fires_nor_pairs_with_next_press() -> None:
    state = HotkeyPressState()
    state.interval_started("screenshot", 1000)
    assert _kinds(state.activated("screenshot", 1000)) == ["fire"]
    state.interval_ended("screenshot", 1600)
    assert state.activated("screenshot", 1620) == ()  # 合成器计时器时间晚于松手的连发
    assert state.interval_started("screenshot", 3000) == ()
    assert state.interval_ended("screenshot", 3050) == ()
    assert _kinds(state.activated("screenshot", 3000)) == ["fire"]


def test_reader_lag_does_not_stop_the_next_recording() -> None:
    state = HotkeyPressState()
    state.interval_started("voice", 1000)
    state.activated("voice", 1000)
    # 第二次按下的放行先于第一次的结束与第二次的开始到达主线程
    assert state.activated("voice", 2000) == ()
    assert _kinds(state.interval_ended("voice", 1500)) == ["voice_stop"]
    assert _kinds(state.interval_started("voice", 2000)) == ["voice_start"]
    assert state.voice_active is True


def test_failed_start_is_not_retried_within_the_same_press() -> None:
    state = HotkeyPressState()
    state.interval_started("voice", 1000)
    assert _kinds(state.activated("voice", 1000)) == ["voice_start"]
    state.start_failed()
    assert state.voice_active is False
    assert state.activated("voice", 1500) == ()
    assert state.activated("voice", 1530) == ()
    assert state.interval_ended("voice", 2000) == ()


def test_timestamps_compare_across_32_bit_wraparound() -> None:
    assert ts_le(0xFFFFFF00, 0x10)
    assert not ts_le(0x10, 0xFFFFFF00)
    state = HotkeyPressState()
    state.interval_started("voice", 0xFFFFFF00)
    assert _kinds(state.activated("voice", 0x10)) == ["voice_start"]
    assert _kinds(state.interval_ended("voice", 0x200)) == ["voice_stop"]


def test_reset_clears_the_combo_so_the_next_press_triggers() -> None:
    state = HotkeyPressState()
    state.interval_started("voice", 1000)
    state.activated("voice", 1000)
    state.reset("voice")
    assert state.voice_active is False
    state.interval_started("voice", 9000)
    assert _kinds(state.activated("voice", 9000)) == ["voice_start"]


def test_stalled_main_thread_still_triggers_both_quick_presses() -> None:
    state = HotkeyPressState()
    for start, end in ((1000, 1060), (1200, 1250)):
        state.interval_started("screenshot", start)
        state.interval_ended("screenshot", end)
    assert _kinds(state.activated("screenshot", 1000)) == ["fire"]
    assert _kinds(state.activated("screenshot", 1200)) == ["fire"]

    state.interval_started("voice", 3000)
    state.interval_ended("voice", 3080)
    state.interval_started("voice", 3200)
    assert _kinds(state.activated("voice", 3000)) == ["voice_start", "voice_stop"]
    assert _kinds(state.activated("voice", 3200)) == ["voice_start"]


def test_untriggered_intervals_ending_before_an_activation_are_dropped() -> None:
    state = HotkeyPressState()
    # 两次按下 GNOME 没有放行（如锁屏），随后第三次按下的放行先到
    for start, end in ((1000, 1050), (2000, 2050)):
        state.interval_started("repeat", start)
        state.interval_ended("repeat", end)
    assert state.activated("repeat", 3000) == ()
    # 前两个区间结束早于 3000，已删除：落在其中的放行不再匹配任何区间
    assert state.activated("repeat", 1000) == ()
    assert _kinds(state.interval_started("repeat", 3000)) == ["fire"]


def test_pending_activations_are_kept_in_order_so_lagging_reader_loses_no_press() -> None:
    state = HotkeyPressState()
    # 读线程滞后超过两次按下的间隔：两条放行都先于任何区间到达
    assert state.activated("ocr", 1000) == ()
    assert state.activated("ocr", 1300) == ()
    assert _kinds(state.interval_started("ocr", 1000)) == ["fire"]
    assert state.interval_ended("ocr", 1100) == ()
    assert _kinds(state.interval_started("ocr", 1300)) == ["fire"]


def test_reader_progress_drops_pending_activations_from_inputs_evdev_cannot_see() -> None:
    state = HotkeyPressState()
    # evdev 看不到的来源（如远程桌面）按住组合：GNOME 连发放行，但没有任何本机区间
    for t in (5000, 5500, 5530):
        assert state.activated("voice", t) == ()
    assert len(state._combos["voice"].pending) == 3  # 只能看内部：清理不改变任何对外边沿
    state.reader_progress(6000)  # 读线程已处理到 6000 的一次本机按键
    assert state._combos["voice"].pending == []
    state.interval_started("voice", 7000)
    assert _kinds(state.activated("voice", 7000)) == ["voice_start"]
    # 落在已触发、未结束区间内的挂起不因进度丢弃（8.16 R3），等结束上报再定
    assert state.activated("voice", 7500) == ()
    state.reader_progress(8000)
    assert state._combos["voice"].pending == [7500]
    assert _kinds(state.interval_ended("voice", 9000)) == ["voice_stop"]
    assert state._combos["voice"].pending == []
