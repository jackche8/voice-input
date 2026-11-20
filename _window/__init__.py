"""主窗口进程（`python -m _window`）：系统网页内核显示本机设置/工作台页面，只信任本应用证书且只限 127.0.0.1。

Linux 用 gi 的 WebKit2GTK 4.1（webkit_gtk），Windows/macOS 用 pywebview（pywebview_host，未经真机）。
后端模块在 __main__ 里按需延迟导入：本包初始化不导入 gi / pywebview。
"""


class BackendMissing(RuntimeError):
    """本机缺少该后端需要的网页内核/绑定（后端在导入阶段抛出，入口以退出码 3 结束并写明原因）。"""
