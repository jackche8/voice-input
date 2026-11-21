"""冲突扫描（只读 gsettings，含五键 ocr / annotate）的假 runner 用例：GNOME 全局快捷键授权表、排除本应用、读取失败进 unread。"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from _config import ConfigStore
from _hotkey import HotkeyBindings
from _linux.conflicts import ConflictScan, parse_global_shortcuts, scan_hotkey_conflicts
from _linux.portal_dbus import DAEMON_APP_ID, LEGACY_APP_ID
from _linux.startup_check import startup_issues
from _paths import AppPaths
from _v3.component_routes import ComponentRegistry
from _v3.production import ProductionEndpoints, SystemAdapters, _setup_service

GS = "org.gnome.settings-daemon.global-shortcuts"
APP_PATH = f"{GS}.application:/org/gnome/settings-daemon/global-shortcuts"
MANGGO = (
    "[('dictation', {'shortcuts': <['<Alt>z']>, 'description': <'语音输入'>}), "
    "('screenshot-recognition', {'shortcuts': <['<Alt>x']>, 'description': <'截图识别'>}), "
    "('ocr-capture', {'shortcuts': <['<Control><Alt>o']>, 'description': <'纯OCR'>}), "
    "('annotate-capture', {'shortcuts': <['<Control><Shift>a']>, 'description': <'截屏标注'>})]"
)
OWN = "[('voice_ctrl_alt_z', {'shortcuts': <['<Control><Alt>z']>, 'description': <'语音输入'>})]"
NO_SHORTCUTS = "[('main_panel_visibility', {'description': <'显示/隐藏窗口'>})]"
BINDINGS = HotkeyBindings(
    voice="ctrl+alt+z", screenshot="alt+x", repeat="ctrl+alt+r", ocr="ctrl+shift+o", annotate="ctrl+alt+a"
)


def fake_run(table: dict[tuple[str, ...], tuple[int, str]], calls: list | None = None):
    def run(cmd, **kw):
        del kw
        if calls is not None:
            calls.append(cmd)
        if tuple(cmd[1:2]) == ("list-recursively",):
            rc, out = table.get(tuple(cmd[1:]), (0, ""))
        else:
            rc, out = table.get(tuple(cmd[1:]), (0, "@as []"))
        return subprocess.CompletedProcess(cmd, rc, out if rc == 0 else "", "" if rc == 0 else out)

    return run


def base_table(apps: str) -> dict:
    return {
        ("get", GS, "applications"): (0, apps),
        ("get", f"{APP_PATH}/com.pylogmon.Manggo/", "shortcuts"): (0, MANGGO),
        ("get", f"{APP_PATH}/{DAEMON_APP_ID}/", "shortcuts"): (0, OWN),
        ("get", f"{APP_PATH}/{LEGACY_APP_ID}/", "shortcuts"): (0, OWN),
        ("get", f"{APP_PATH}/wechat/", "shortcuts"): (0, NO_SHORTCUTS),
    }


def test_global_shortcuts_conflict_reported_and_own_app_excluded():
    calls: list = []
    apps = f"['{DAEMON_APP_ID}', '{LEGACY_APP_ID}', 'wechat', 'com.pylogmon.Manggo']"
    scan = scan_hotkey_conflicts(BINDINGS, run=fake_run(base_table(apps), calls), which=lambda n: "/usr/bin/gsettings")
    assert scan.unread == ()
    assert scan.conflicts == ("screenshot alt+x 与 应用 com.pylogmon.Manggo 的全局快捷键 screenshot-recognition 的 <Alt>x 冲突",)
    read_apps = [c[2] for c in calls if c[1] == "get" and c[2].startswith(APP_PATH)]
    assert all(DAEMON_APP_ID not in a and not a.endswith(f"/{LEGACY_APP_ID}/") for a in read_apps), read_apps


def test_fourth_hotkey_ocr_conflict_and_duplicate_startup_check():
    apps = "['com.pylogmon.Manggo']"
    ocr_conflict_bindings = HotkeyBindings(
        voice="ctrl+alt+z", screenshot="ctrl+alt+x", repeat="ctrl+alt+r", ocr="ctrl+alt+o"
    )
    scan = scan_hotkey_conflicts(ocr_conflict_bindings, run=fake_run(base_table(apps)), which=lambda n: "/usr/bin/gsettings")
    assert scan.conflicts == ("ocr ctrl+alt+o 与 应用 com.pylogmon.Manggo 的全局快捷键 ocr-capture 的 <Control><Alt>o 冲突",)

    dup_bindings = HotkeyBindings(
        voice="ctrl+alt+z", screenshot="ctrl+alt+x", repeat="ctrl+alt+r", ocr="ctrl+alt+x"
    )
    issues = startup_issues(
        dup_bindings,
        environ={"XDG_SESSION_TYPE": "x11"},
        which=lambda n: f"/usr/bin/{n}",
        portal_version=lambda _i: 1,
        screenshot_tool="maim",
        conflicts=lambda _b: ConflictScan((), ()),
    )
    assert any("快捷键重复" in item and "ocr" in item for item in issues), issues


def test_fifth_hotkey_annotate_conflict_duplicate_and_production_scan(tmp_path: Path):
    apps = "['com.pylogmon.Manggo']"
    ann_conflict_bindings = HotkeyBindings(
        voice="ctrl+alt+z", screenshot="ctrl+alt+x", repeat="ctrl+alt+r", ocr="ctrl+shift+o", annotate="ctrl+shift+a"
    )
    scan = scan_hotkey_conflicts(ann_conflict_bindings, run=fake_run(base_table(apps)), which=lambda n: "/usr/bin/gsettings")
    assert scan.conflicts == (
        "annotate ctrl+shift+a 与 应用 com.pylogmon.Manggo 的全局快捷键 annotate-capture 的 <Control><Shift>a 冲突",
    )

    dup_bindings = HotkeyBindings(
        voice="ctrl+alt+z", screenshot="ctrl+alt+x", repeat="ctrl+alt+r", ocr="ctrl+alt+o", annotate="ctrl+alt+x"
    )
    issues = startup_issues(
        dup_bindings,
        environ={"XDG_SESSION_TYPE": "x11"},
        which=lambda n: f"/usr/bin/{n}",
        portal_version=lambda _i: 1,
        screenshot_tool="maim",
        conflicts=lambda _b: ConflictScan((), ()),
    )
    assert any("快捷键重复" in item and "annotate" in item for item in issues), issues

    # 生产装配 _v3.production._setup_service 的 hotkey_scan 必须把配置里的 hotkey_annotate 传进 scan_hotkey_conflicts
    paths = AppPaths.for_roots(config_root=tmp_path / "cfg", data_root=tmp_path / "dat", runtime_dir=tmp_path / "run")
    paths.config_root.mkdir(parents=True)
    paths.config.write_text(json.dumps({"hotkey_annotate": "ctrl+shift+a"}), encoding="utf-8")
    adapters = SystemAdapters(
        environ={"XDG_SESSION_TYPE": "x11"},
        run=fake_run(base_table(apps)),
        which=lambda n: f"/usr/bin/{n}",
        portal_version=lambda _i: (1, None),
        ocr_languages=lambda: ["eng", "chi_sim", "chi_tra"],
        keyboard_devices=lambda: [],
    )
    service = _setup_service(paths, ProductionEndpoints(), adapters, ConfigStore(paths.config), ComponentRegistry())
    caps = {cap.id: cap for cap in service._probes.capabilities("x11")}
    assert caps["hotkey_conflicts"].available is False
    assert "annotate ctrl+shift+a" in caps["hotkey_conflicts"].detail


def test_entries_without_shortcuts_are_not_guessed():
    assert parse_global_shortcuts(NO_SHORTCUTS) == []
    assert parse_global_shortcuts("@a(sa{sv}) []") == []
    assert parse_global_shortcuts(MANGGO) == [
        ("dictation", ["<Alt>z"]),
        ("screenshot-recognition", ["<Alt>x"]),
        ("ocr-capture", ["<Control><Alt>o"]),
        ("annotate-capture", ["<Control><Shift>a"]),
    ]


def test_read_failures_go_to_unread_not_no_conflict():
    table = base_table("['com.pylogmon.Manggo', 'broken']")
    table[("list-recursively", "org.gnome.mutter.wayland.keybindings")] = (1, "No such schema “org.gnome.mutter.wayland.keybindings”")
    table[("get", f"{APP_PATH}/broken/", "shortcuts")] = (0, "[('x', {'shortcuts': <oops>})]")
    scan = scan_hotkey_conflicts(BINDINGS, run=fake_run(table), which=lambda n: "/usr/bin/gsettings")
    assert len(scan.conflicts) == 1
    assert any("org.gnome.mutter.wayland.keybindings" in u and "No such schema" in u for u in scan.unread), scan.unread
    assert any("broken" in u for u in scan.unread), scan.unread


def test_missing_global_shortcuts_schema_is_unread():
    table = {("get", GS, "applications"): (1, "No such schema “org.gnome.settings-daemon.global-shortcuts”")}
    scan = scan_hotkey_conflicts(BINDINGS, run=fake_run(table), which=lambda n: "/usr/bin/gsettings")
    assert scan.conflicts == ()
    assert any("全局快捷键授权表" in u for u in scan.unread), scan.unread


def test_runner_exception_is_unread():
    def run(cmd, **kw):
        del kw
        raise subprocess.TimeoutExpired(cmd, 2)

    scan = scan_hotkey_conflicts(BINDINGS, run=run, which=lambda n: "/usr/bin/gsettings")
    assert scan.conflicts == ()
    assert len(scan.unread) == 5 + 1 + 1
    assert all("TimeoutExpired" in u for u in scan.unread)
