"""OCR 样本唯一生成器（T11）：合成截图 + 逐行真值（行框、块号、软/硬换行、缩进）。

正文用比例字体（DejaVu Sans / Noto Sans CJK），代码与终端用等宽字体（DejaVu Sans Mono），按浏览器同理的贪心折行
排版；真值行框是 PIL 量出的墨迹框（与 Tesseract 词框同口径）。字体缺失时 `fonts_missing()` 给出原因，用例据此 skip。
全部样本确定性生成、进程内缓存。
"""
from __future__ import annotations

import difflib
import io
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SANS = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
CJK = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
CJK_SC, CJK_TC = 2, 3  # NotoSansCJK-Regular.ttc 内的简体 / 繁体字面


@dataclass(frozen=True)
class TruthLine:
    text: str
    bbox: tuple[int, int, int, int]  # (x, y, w, h) 墨迹框，原图像素
    block: int
    soft_wrap: bool
    indent: int = 0  # 相对块左缘的缩进空格数（只对等宽代码有意义）


@dataclass(frozen=True)
class OcrSample:
    name: str
    png: bytes
    size: tuple[int, int]
    lines: tuple[TruthLine, ...]

    @property
    def words(self) -> int:
        return sum(len(line.text.split()) for line in self.lines)


BBOX_TOLERANCE_PX = 3  # 六.2：行框偏差 ≤3px
TEXT_SIMILARITY_MIN = 0.9  # 去空白后的字符相似度：容许个别字符识别错（如 <= 认成 <），不容许缺行或串行


def bbox_close(found: tuple[int, int, int, int], truth: tuple[int, int, int, int]) -> bool:
    """两个 (x, y, w, h) 框的四条边都在容差内。"""
    fx, fy, fw, fh = found
    tx, ty, tw, th = truth
    edges = zip((fx, fy, fx + fw, fy + fh), (tx, ty, tx + tw, ty + th))
    return all(abs(a - b) <= BBOX_TOLERANCE_PX for a, b in edges)


def text_similar(found: str, truth: str) -> bool:
    a, b = "".join(found.split()), "".join(truth.split())
    return difflib.SequenceMatcher(None, a, b).ratio() >= TEXT_SIMILARITY_MIN


def fonts_missing() -> str | None:
    missing = [path for path in (SANS, MONO, CJK) if not Path(path).is_file()]
    return f"缺少样本字体：{', '.join(missing)}" if missing else None


def _font(path: str, size: int, index: int = 0) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size, index=index)


def _is_cjk(ch: str) -> bool:
    return "㐀" <= ch <= "鿿" or "　" <= ch <= "〿" or "＀" <= ch <= "￯"


@dataclass
class _Canvas:
    size: tuple[int, int]
    bg: tuple[int, int, int]
    lines: list[TruthLine] = field(default_factory=list)
    block: int = -1

    def __post_init__(self) -> None:
        self.image = Image.new("RGB", self.size, self.bg)
        self.draw = ImageDraw.Draw(self.image)

    def fill(self, box: tuple[int, int, int, int], color: tuple[int, int, int]) -> None:
        self.draw.rectangle(box, fill=color)

    def new_block(self) -> None:
        self.block += 1

    def line(self, x: int, y: int, text: str, font, fg, *, soft: bool = False, indent: int = 0) -> None:
        self.draw.text((x, y), text, font=font, fill=fg)
        mask, (dx, dy) = font.getmask2(text, anchor="la")  # 墨迹框（textbbox 的横向是步进宽度，不是墨迹）
        ix0, iy0, ix1, iy1 = mask.getbbox()
        bbox = (x + dx + ix0, y + dy + iy0, ix1 - ix0, iy1 - iy0)
        self.lines.append(TruthLine(text, bbox, self.block, soft, indent))

    def paragraph(self, x: int, y: int, width: int, text: str, font, fg, pitch: int) -> int:
        """贪心折行：放不下的词（中文按字）换到下一行；首行硬、其余软。返回段后的 y。"""
        rows = _wrap(text, font, width)
        for i, row in enumerate(rows):
            self.line(x, y + i * pitch, row, font, fg, soft=i > 0)
        return y + len(rows) * pitch

    def code(self, x: int, y: int, rows: list[str], font, fg, pitch: int) -> int:
        advance = font.getlength(" ")
        for i, row in enumerate(rows):
            indent = len(row) - len(row.lstrip(" "))
            self.line(round(x + indent * advance), y + i * pitch, row.strip(" "), font, fg, indent=indent)
        return y + len(rows) * pitch

    def sample(self, name: str) -> OcrSample:
        buf = io.BytesIO()
        self.image.save(buf, format="PNG")
        return OcrSample(name, buf.getvalue(), self.size, tuple(self.lines))


def _wrap(text: str, font, width: int) -> list[str]:
    units = list(text) if _is_cjk(text[0]) else text.split(" ")
    sep = "" if _is_cjk(text[0]) else " "
    rows: list[str] = []
    current = ""
    for unit in units:
        candidate = f"{current}{sep}{unit}" if current else unit
        if current and font.getlength(candidate) > width:
            rows.append(current)
            current = unit
        else:
            current = candidate
    rows.append(current)
    return rows


DARK_FG, LIGHT_FG = (31, 41, 55), (229, 231, 235)

EN = [
    "The installer downloads the speech model weights, verifies every checksum against the published "
    "manifest and only then starts the recognition service in the background.",
    "Restart the desktop daemon after you change a hotkey so that the new binding is registered with the "
    "compositor and the old one is released.",
    "Selections larger than the screen are clipped to the visible area, and the recognized lines are drawn "
    "back at their original positions with a translucent background.",
    "When the translation service is busy, requests are queued and processed in order, and a short notice "
    "explains how long the current batch is expected to take.",
    "History entries are stored locally on this computer and can be exported as plain text or removed "
    "individually from the settings page at any time.",
]
ZH_SC = [
    "安装程序会先下载语音模型权重，并逐个核对校验值，确认无误后才在后台启动识别服务，整个过程不需要手动干预。",
    "修改快捷键之后请重新启动桌面守护进程，新的组合键才会向合成器登记，旧的组合键也会同时释放。",
]
ZH_TC = [
    "安裝程式會先下載語音模型權重，並逐一核對校驗值，確認無誤後才在背景啟動辨識服務，整個過程不需要手動干預。",
    "修改快速鍵之後請重新啟動桌面常駐程式，新的組合鍵才會向合成器登記，舊的組合鍵也會同時釋放。",
    "選取範圍超出螢幕時會裁切到可見區域，辨識出的每一行文字會以半透明底色畫回原本的位置。",
]
CODE = [
    "def load_settings(path):",
    "    with open(path, encoding='utf-8') as fh:",
    "        data = json.load(fh)",
    "    # read the timeout from the settings file",
    "    timeout = data.get('timeout', 15)  # seconds before giving up",
    "    if timeout <= 0:",
    "        raise ValueError('timeout must be positive')",
    "    return Settings(timeout=timeout)",
]
TERMINAL = [
    "$ sudo apt install tesseract-ocr-chi-tra tesseract-ocr-eng",
    "$ systemctl --user restart ollama-voice-daemon",
    "$ journalctl --user -u ollama-voice-daemon -f",
]


def _light_paragraph() -> OcrSample:
    c = _Canvas((900, 260), (255, 255, 255))
    font = _font(SANS, 15)
    y = 20
    for text in EN[:2]:
        c.new_block()
        y = c.paragraph(24, y, 840, text, font, DARK_FG, 22) + 18
    return c.sample("light_paragraph")


def _dark_paragraph() -> OcrSample:
    c = _Canvas((900, 200), (17, 24, 39))
    font = _font(SANS, 14)
    c.new_block()
    y = c.paragraph(24, 20, 840, EN[2], font, LIGHT_FG, 21) + 18
    c.new_block()
    c.line(24, y, "Press Esc to close the window.", font, LIGHT_FG)
    return c.sample("dark_paragraph")


def _code() -> OcrSample:
    c = _Canvas((760, 230), (246, 248, 250))
    c.new_block()
    c.code(24, 20, CODE, _font(MONO, 14), (36, 41, 47), 22)
    return c.sample("code")


def _terminal() -> OcrSample:
    c = _Canvas((760, 120), (12, 12, 12))
    c.new_block()
    c.code(20, 20, TERMINAL, _font(MONO, 14), (204, 204, 204), 24)
    return c.sample("terminal")


def _menu() -> OcrSample:
    c = _Canvas((300, 170), (250, 250, 250))
    font = _font(SANS, 14)
    c.new_block()
    for i, item in enumerate(["New Window", "Open File...", "Settings", "History", "About"]):
        c.line(24, 16 + i * 28, item, font, DARK_FG)
    return c.sample("menu")


def _table() -> OcrSample:
    c = _Canvas((640, 170), (255, 255, 255))
    font = _font(SANS, 14)
    rows = [("Name", "Size", "Updated"), ("model.safetensors", "4.4 GB", "2026-11-02"),
            ("tokenizer.json", "11 MB", "2026-11-02"), ("config.json", "1.9 KB", "2026-10-28")]
    for col, x in enumerate((24, 300, 460)):
        c.new_block()
        for r, row in enumerate(rows):
            c.line(x, 20 + r * 28, row[col], font, DARK_FG)
    return c.sample("table")


def _hanging_list() -> OcrSample:
    c = _Canvas((600, 210), (255, 255, 255))
    font = _font(SANS, 14)
    items = [
        "Download the release archive from the project page and unpack it into any folder you like.",
        "Run the installer, which checks the system packages and prepares an isolated environment.",
        "Open the settings page in the browser to finish the setup.",
    ]
    c.new_block()
    y = 20
    for n, item in enumerate(items, 1):
        marker = f"{n})"  # 「1.」式单个数字编号在 Tesseract 默认版面分析下会丢数字（实测只剩「.」），见 O1 报告
        text_x = 24 + round(font.getlength(marker + " "))
        rows = _wrap(item, font, 540 - text_x)
        c.line(24, y, f"{marker} {rows[0]}", font, DARK_FG)
        for k, row in enumerate(rows[1:], 1):
            c.line(text_x, y + k * 21, row, font, DARK_FG, soft=True)
        y += len(rows) * 21
    return c.sample("hanging_list")


def _chinese(name: str, texts: list[str], index: int) -> OcrSample:
    c = _Canvas((720, 230), (255, 255, 255))
    font = _font(CJK, 16, index)
    y = 20
    for text in texts[:2]:
        c.new_block()
        y = c.paragraph(24, y, 660, text, font, DARK_FG, 26) + 20
    return c.sample(name)


def _two_columns() -> OcrSample:
    c = _Canvas((900, 200), (255, 255, 255))
    font = _font(SANS, 14)
    for x, text in ((24, EN[3]), (484, EN[4])):
        c.new_block()
        c.paragraph(x, 20, 390, text, font, DARK_FG, 21)
    return c.sample("two_columns")


def _article(name: str, size: tuple[int, int], font, texts: list[str], *, columns: int, bg, fg, pitch: int,
             words: int | None = None) -> OcrSample:
    """多栏正文铺满画面（words 给定时铺到该词数为止）。"""
    c = _Canvas(size, bg)
    margin, gutter = 30, 40
    col_w = (size[0] - 2 * margin - gutter * (columns - 1)) // columns
    count, k = 0, 0
    for col in range(columns):
        x, y = margin + col * (col_w + gutter), margin
        while y + pitch * 4 < size[1] - margin and (words is None or count < words):
            text = texts[k % len(texts)]
            rows = _wrap(text, font, col_w)
            if y + len(rows) * pitch > size[1] - margin:
                break
            c.new_block()
            y = c.paragraph(x, y, col_w, text, font, fg, pitch) + pitch
            count += len(text.split())
            k += 1
    return c.sample(name)


@cache
def all_samples() -> dict[str, OcrSample]:
    """六.2 的九类 + 终端 + 繁体段落；另有三张整屏（全屏密集、英文整屏 ≥400 词、繁体整屏）。"""
    sans14 = _font(SANS, 14)
    built = [
        _light_paragraph(), _dark_paragraph(), _code(), _terminal(), _menu(), _table(), _hanging_list(),
        _chinese("chinese_paragraph", ZH_SC, CJK_SC), _chinese("traditional_paragraph", ZH_TC, CJK_TC),
        _two_columns(),
        _article("large_dark_small_text", (1280, 860), _font(SANS, 12), EN, columns=2, bg=(30, 30, 30),
                 fg=(212, 212, 212), pitch=18),
        _article("fullscreen_dense", (3440, 1440), sans14, EN, columns=6, bg=(255, 255, 255), fg=DARK_FG, pitch=20),
        _article("fullscreen_article", (1920, 1080), _font(SANS, 15), EN, columns=3, bg=(255, 255, 255),
                 fg=DARK_FG, pitch=22, words=440),
        _article("fullscreen_traditional", (1920, 1080), _font(CJK, 18, CJK_TC), ZH_TC, columns=3,
                 bg=(255, 255, 255), fg=DARK_FG, pitch=30),
    ]
    return {sample.name: sample for sample in built}


@cache
def dark_low_contrast_sample() -> OcrSample:
    """深底低对比度灰字合成样本（原图默认识别为空，启用高对比预设可命中早停且保持原底色 #181e2c 采样）。"""
    c = _Canvas((420, 72), (24, 30, 44))
    c.new_block()
    c.line(24, 24, "Dark Mode Status: Ready 2026", _font(SANS, 16), (42, 48, 62))
    return c.sample("dark_low_contrast")


ACCURACY_SAMPLES = (
    "light_paragraph", "dark_paragraph", "code", "terminal", "menu", "table", "hanging_list",
    "chinese_paragraph", "traditional_paragraph", "two_columns", "large_dark_small_text",
)
FULLSCREEN_SAMPLES = ("fullscreen_dense", "fullscreen_article", "fullscreen_traditional")
