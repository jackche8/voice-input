"""桌面业务流程：把跨平台能力（`_platform`）、原生 Qt 视图（`_ui_desktop`）与本机 v1 API 组装成功能。

- 语音输入：按下绑定焦点 + 串行队列推流（含热词 context，杜绝连按 409）→ 松开 finish → POST /api/v1/utterances；
- 投递循环：jobs claim → 按 job 焦点注入 → ack；重复键与未确认重发走 /api/v1/utterances/resend；
- 截屏原位翻译与纯 OCR：100% 复用全屏红十字框选器 → POST /api/v1/ocr-inplace；
- 产品状态流：后台 SSE 指数退避自动重连驱动左下角状态胶囊与双行大字幕。
本包不导入 GTK/WebKit 或具体图形控件，测试可用假对象无头驱动。
"""
