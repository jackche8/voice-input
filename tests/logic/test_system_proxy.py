"""跨平台 OS 系统代理读取、Go 协议过滤与 NO_PROXY 回环合并单元测试（不触网、不读宿主机真实注册表）。"""
from __future__ import annotations

from _platform.system_proxy import (
    WINDOWS_INTERNET_SETTINGS_KEY,
    format_proxy_hint,
    merge_proxy_env,
    normalize_go_proxy_url,
    read_os_system_proxy,
    read_windows_system_proxy,
    redact_url_credentials,
)


class _FakeRegKey:
    def __init__(self, values: dict[str, object]) -> None:
        self.values = values

    def __enter__(self) -> "_FakeRegKey":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _FakeWinReg:
    HKEY_CURRENT_USER = object()

    def __init__(self, values: dict[str, object] | None = None, *, open_error: Exception | None = None) -> None:
        self._values = dict(values or {})
        self._open_error = open_error

    def OpenKey(self, root: object, subkey: str) -> _FakeRegKey:
        assert root is self.HKEY_CURRENT_USER
        assert subkey == WINDOWS_INTERNET_SETTINGS_KEY
        if self._open_error is not None:
            raise self._open_error
        return _FakeRegKey(self._values)

    def QueryValueEx(self, key: _FakeRegKey, name: str) -> tuple[object, int]:
        if name not in key.values:
            raise FileNotFoundError(name)
        return key.values[name], 1


def test_windows_registry_proxy_parsing_and_filtering():
    # 1. ProxyEnable=0 忽略
    reg_off = _FakeWinReg({"ProxyEnable": 0, "ProxyServer": "127.0.0.1:7890", "ProxyOverride": "*.corp;<local>"})
    assert read_windows_system_proxy(reg_api=reg_off) == ({}, [])

    # 2. 单值 host:port、尾分号与 IPv6 均补 http:// 同时赋给 http/https，ProxyOverride 分号转逗号并去 <local>
    reg_single = _FakeWinReg({
        "ProxyEnable": 1,
        "ProxyServer": "127.0.0.1:7890;",
        "ProxyOverride": "*.corp; 10.0.0.1 ;<LOCAL>;*.corp",
    })
    assert read_windows_system_proxy(reg_api=reg_single) == (
        {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"},
        ["*.corp", "10.0.0.1"],
    )

    reg_ipv6 = _FakeWinReg({"ProxyEnable": 1, "ProxyServer": "[::1]:7890"})
    assert read_windows_system_proxy(reg_api=reg_ipv6) == (
        {"http": "http://[::1]:7890", "https": "http://[::1]:7890"},
        [],
    )

    # 3. 分协议：裸 https= 必须补 http://（HTTP CONNECT），显式 https:// / socks5:// / socks5h:// 原样保留，尾分号容错
    reg_split = _FakeWinReg({
        "ProxyEnable": 1,
        "ProxyServer": "http=127.0.0.1:7890;https=127.0.0.1:7891;ftp=127.0.0.1:2121;",
    })
    assert read_windows_system_proxy(reg_api=reg_split) == (
        {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7891"},
        [],
    )

    reg_explicit_https = _FakeWinReg({
        "ProxyEnable": 1,
        "ProxyServer": "http=socks5://127.0.0.1:7891;https=https://proxy.corp:8443",
    })
    assert read_windows_system_proxy(reg_api=reg_explicit_https) == (
        {"http": "socks5://127.0.0.1:7891", "https": "https://proxy.corp:8443"},
        [],
    )

    reg_socks5h = _FakeWinReg({"ProxyEnable": 1, "ProxyServer": "socks5h://127.0.0.1:1080"})
    assert read_windows_system_proxy(reg_api=reg_socks5h) == (
        {"http": "socks5h://127.0.0.1:1080", "https": "socks5h://127.0.0.1:1080"},
        [],
    )

    # 4. 严禁转发 socks=（WinINet 默认 SOCKS4）、socks4:// 与各类畸形值
    reg_socks4 = _FakeWinReg({"ProxyEnable": 1, "ProxyServer": "socks=127.0.0.1:1080", "ProxyOverride": "*.corp"})
    assert read_windows_system_proxy(reg_api=reg_socks4) == ({}, [])

    for bad in (
        "",
        "   ",
        "socks4://127.0.0.1:1080",
        "socks4a://127.0.0.1:1080",
        "socks://127.0.0.1:1080",
        "ftp://127.0.0.1:21",
        "http://127.0.0.1:",
        "http://[::1]:",
        "http://127.0.0.1:0",
        "http://127.0.0.1:70000",
        "http://:7890",
        "http://127.0.0.1:7890/bad",
        "http://127.0.0.1:7890?a=1",
        "http://127.0.0.1:7890#frag",
        "http://127.0.0.1:7890 bad",
        "http://127.0.0.1:7890\nX",
    ):
        assert normalize_go_proxy_url(bad) is None, bad

    # 5. 注册表读取异常 fail-open
    reg_broken = _FakeWinReg(open_error=OSError("registry unavailable"))
    assert read_windows_system_proxy(reg_api=reg_broken) == ({}, [])


def test_merge_proxy_env_precedence_no_proxy_and_fail_open():
    # 1. macOS 经 SystemConfiguration 读取并过滤非 Go 协议；Linux 返回空
    mac_proxies, mac_overrides = read_os_system_proxy(
        "macos",
        macos_reader=lambda: {
            "http": "http://127.0.0.1:7890",
            "https": "socks4://127.0.0.1:1080",
        },
    )
    assert mac_proxies == {"http": "http://127.0.0.1:7890"} and mac_overrides == []
    assert read_os_system_proxy("macos", macos_reader=lambda: (_ for _ in ()).throw(RuntimeError("boom"))) == ({}, [])
    assert read_os_system_proxy("linux") == ({}, [])

    # 2. 用户已显式配置 HTTP 与 HTTPS 代理时，完全跳过系统代理读取，仍保底合并回环到 NO_PROXY
    calls = {"n": 0}

    def spy_reader(_os_name):
        calls["n"] += 1
        return ({"http": "http://os:1", "https": "http://os:2"}, ["os.local"])

    merged_explicit = merge_proxy_env(
        {"HTTP_PROXY": "http://user:8080", "HTTPS_PROXY": "http://user:8443"},
        os_name="macos",
        read_system_proxy=spy_reader,
    )
    assert calls["n"] == 0
    assert merged_explicit["HTTP_PROXY"] == "http://user:8080"
    assert merged_explicit["HTTPS_PROXY"] == "http://user:8443"
    assert merged_explicit["NO_PROXY"] == "127.0.0.1,localhost,::1"
    assert "no_proxy" not in merged_explicit

    # 3. POSIX 下用户显式小写 http_proxy + no_proxy：不覆盖 http_proxy，同步更新 NO_PROXY 与 no_proxy
    merged_posix = merge_proxy_env(
        {"http_proxy": "http://user:7890", "no_proxy": "corp.local,localhost"},
        os_name="macos",
        read_system_proxy=lambda _os: ({"http": "http://os:1", "https": "http://os:2"}, []),
    )
    assert merged_posix["http_proxy"] == "http://user:7890"
    assert "HTTP_PROXY" not in merged_posix
    assert merged_posix["HTTPS_PROXY"] == "http://os:2"
    assert merged_posix["NO_PROXY"] == "corp.local,localhost,127.0.0.1,::1"
    assert merged_posix["no_proxy"] == "corp.local,localhost,127.0.0.1,::1"

    # 4. Windows 下大小写不敏感合并并只保留规范大写键（清理小写同义键防 CreateProcessW 重复键），合并 ProxyOverride
    merged_win = merge_proxy_env(
        {"http_proxy": "http://user:7890", "no_proxy": "intranet.local"},
        os_name="windows",
        read_system_proxy=lambda _os: ({"http": "http://os:1", "https": "http://os:7891"}, ["*.corp", "10.0.0.1"]),
    )
    assert merged_win["HTTP_PROXY"] == "http://user:7890"
    assert merged_win["HTTPS_PROXY"] == "http://os:7891"
    assert merged_win["NO_PROXY"] == "intranet.local,*.corp,10.0.0.1,127.0.0.1,localhost,::1"
    assert "http_proxy" not in merged_win and "no_proxy" not in merged_win

    # 5. 无任何代理时不污染 NO_PROXY；系统读取器抛异常时 fail-open 不阻断
    plain = merge_proxy_env({"PATH": "/usr/bin"}, os_name="windows", read_system_proxy=lambda _os: ({}, []))
    assert plain == {"PATH": "/usr/bin"}

    def crashing_reader(_os_name):
        raise RuntimeError("sysconf crashed")

    safe = merge_proxy_env({"PATH": "/usr/bin"}, os_name="macos", read_system_proxy=crashing_reader)
    assert safe == {"PATH": "/usr/bin"}


def test_redact_url_credentials_and_format_proxy_hint():
    # 1. URL 凭证脱敏：单 URL、多 URL、密码含 @、IPv6，不误伤路径/查询/片段里的 @
    assert redact_url_credentials("") == ""
    assert redact_url_credentials("http://alice:s3cr3t@127.0.0.1:7890") == "http://***@127.0.0.1:7890"
    assert redact_url_credentials("http://alice:p@ss@127.0.0.1:7890/api") == "http://***@127.0.0.1:7890/api"
    assert redact_url_credentials("socks5h://tok@[::1]:1080") == "socks5h://***@[::1]:1080"
    assert (
        redact_url_credentials("failed http://u1:p1@a.local:8080 and https://u2:p2@b.local:8443/x")
        == "failed http://***@a.local:8080 and https://***@b.local:8443/x"
    )
    assert redact_url_credentials("https://example.com/path?email=a@b.com") == "https://example.com/path?email=a@b.com"
    assert redact_url_credentials("https://example.com?email=a@b.com") == "https://example.com?email=a@b.com"
    assert redact_url_credentials("https://example.com#a@b") == "https://example.com#a@b"

    # 2. format_proxy_hint：无代理返回 None；单代理/双代理去重并脱敏凭证；支持 IPv6、ALL_PROXY、Windows 异大小写与畸形值不泄密
    assert format_proxy_hint({"PATH": "/usr/bin"}, os_name="windows") is None
    assert (
        format_proxy_hint(
            {"HTTP_PROXY": "http://alice:secret@127.0.0.1:7890", "HTTPS_PROXY": "http://alice:secret@127.0.0.1:7890"},
            os_name="windows",
        )
        == "127.0.0.1:7890"
    )
    assert (
        format_proxy_hint(
            {"https_proxy": "http://u:p@127.0.0.1:7891", "http_proxy": "http://u:p@127.0.0.1:7890"},
            os_name="macos",
        )
        == "127.0.0.1:7891, 127.0.0.1:7890"
    )
    assert format_proxy_hint({"All_Proxy": "socks5h://tok@[::1]:1080"}, os_name="windows") == "[::1]:1080"
    bad_hint = format_proxy_hint({"HTTPS_PROXY": "http://alice:topsecret@127.0.0.1:99999"}, os_name="linux")
    assert bad_hint is not None and "topsecret" not in bad_hint and "***@" in bad_hint
