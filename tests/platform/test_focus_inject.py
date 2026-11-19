from __future__ import annotations

import pytest

from _desktop.delivery import PlatformFocusProbe, PlatformInjector
from _focus import StaticFocusProbe, decide_focus
from _inject import ClaimAckLog, FocusBoundInjector, InjectEvidence, RecordingBackend, ack_from_evidence
from _platform.protocol import FocusTarget, PasteResult


def test_focus_match_mismatch_unknown():
    assert decide_focus(12, 12) == "match"
    assert decide_focus(12, 13) == "mismatch"
    assert decide_focus(12, 0) == "unknown"
    assert decide_focus(None, 12) == "unknown"


def test_inject_requires_matching_focus_and_ack_evidence():
    probe = StaticFocusProbe(focus_id=7)
    backend = RecordingBackend()
    inj = FocusBoundInjector(probe, backend)
    result, evidence = inj.inject("hello", focus_id=7, claim_id="c1")
    assert result == "complete"
    assert backend.sent == ["hello"]
    result2, evidence2 = inj.inject("nope", focus_id=8, claim_id="c2")
    assert result2 == "none"
    assert evidence2.proved_none is True
    assert backend.sent == ["hello"]
    unknown = FocusBoundInjector(StaticFocusProbe(0), RecordingBackend()).inject("x", focus_id=7)
    # 焦点未知且假后端证明了输出 → complete；改为无法证明时才 unknown
    assert unknown[0] in {"complete", "partial_or_unknown"}


def test_unknown_output_is_not_failed():
    ev = InjectEvidence(False, False, False, method="ibus")
    assert ack_from_evidence(ev) == "partial_or_unknown"
    log = ClaimAckLog()
    assert log.record("c", "partial_or_unknown") == "partial_or_unknown"
    assert log.record("c", "partial_or_unknown") == "partial_or_unknown"
    with pytest.raises(ValueError):
        log.record("c", "complete")


def test_platform_focus_probe_and_injector_support_cross_platform_handles():
    class FakePlatform:
        def __init__(self, focus: FocusTarget, result: PasteResult) -> None:
            self.focus = focus
            self.result = result
            self.calls: list[tuple[str, FocusTarget | None]] = []

        def current_focus(self) -> FocusTarget:
            return self.focus

        def inject_text(self, text: str, *, target: FocusTarget | None = None, terminal=None) -> PasteResult:
            del terminal
            self.calls.append((text, target))
            return self.result

    plat = FakePlatform(
        FocusTarget(focus_id="com.apple.TextEdit:42", app_id="com.apple.TextEdit", is_terminal=False, is_self=False),
        PasteResult(ok=True, method="cgevent_cmd_v", detail="ok"),
    )
    probe = PlatformFocusProbe(plat)
    assert probe.current_id() == "com.apple.TextEdit:42"
    assert probe.is_terminal() is False
    snap = probe.snapshot()
    assert snap.focus_id == "com.apple.TextEdit:42" and snap.terminal is False

    injector = PlatformInjector(plat)
    res, ev = injector.inject("你好", focus_id="com.apple.TextEdit:42", claim_id="c-mac-1")
    assert res == "complete" and ev.proved_output is True and ev.method == "cgevent_cmd_v"
    assert plat.calls[-1][1] is not None and plat.calls[-1][1].focus_id == "com.apple.TextEdit:42"

    plat.result = PasteResult(ok=False, method="focus_mismatch", detail="mismatch", uncertain=False)
    res2, ev2 = injector.inject("跳过", focus_id="other:99", claim_id="c-mac-2")
    assert res2 == "none" and ev2.proved_none is True and ev2.focus_decision == "mismatch"

    plat.result = PasteResult(ok=False, method="sendinput", detail="midway", uncertain=True)
    res3, ev3 = injector.inject("未知", focus_id=None, claim_id="c-mac-3")
    assert res3 == "partial_or_unknown" and ev3.proved_output is False and ev3.proved_none is False
