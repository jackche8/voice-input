"""T9 行分块与软/硬换行：按 4.2 分段规则与等宽块规则逐条（纯几何）。

行框按字宽模型构造：比例字体每个字符宽度不同（窄字 3–4px、宽字 11px、其余 7px），等宽字体每字 8px，
中文每字 14px；正文由贪心折行生成，与浏览器排版同理。
"""
from __future__ import annotations

from _ocr_segment import LineSpan, segment_lines

_NARROW, _WIDE = set("iljtfr.,:;!'|()[]"), set("mwMW@")
SPACE = 4


def _char_w(ch: str, mono: bool) -> int:
    if mono:
        return 8
    if "一" <= ch <= "鿿":
        return 14
    return 3 if ch in _NARROW else 11 if ch in _WIDE else 9 if ch.isupper() else 7


def _word_w(word: str, mono: bool) -> int:
    return sum(_char_w(ch, mono) for ch in word)


def span(x: int, y: int, text: str, *, mono: bool = False, h: int = 14) -> LineSpan:
    """文字 text 从 (x, y) 起；行首空格只用于定位（不进 text），词框按空格切分。"""
    space = _char_w(" ", True) if mono else SPACE
    x += (len(text) - len(text.lstrip(" "))) * space
    words, pos = [], x
    for word in text.strip(" ").split(" "):
        words.append((pos, pos + _word_w(word, mono), word))
        pos += _word_w(word, mono) + space
    return LineSpan(x, y, words[-1][1], y + h, text.strip(" "), tuple(words))


def wrap(text: str, x: int, y: int, width: int, *, pitch: int = 20) -> list[LineSpan]:
    """按宽度贪心折行（放不下的词换到下一行），每行一个 LineSpan。"""
    lines, current = [], []
    for word in text.split(" "):
        candidate = " ".join([*current, word])
        if current and _word_w(candidate.replace(" ", ""), False) + SPACE * len(current) > width:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    lines.append(" ".join(current))
    return [span(x, y + i * pitch, line) for i, line in enumerate(lines)]


def shape(blocks) -> list[list[tuple[str, bool]]]:
    return [[(line.text, line.soft_wrap) for line in block] for block in blocks]


PARA_1 = "The installer downloads the model weights and verifies every checksum before the service starts."
PARA_2 = "Restart the daemon after changing a hotkey."


def test_wrapped_prose_lines_are_soft_and_a_new_sentence_after_a_short_line_is_hard() -> None:
    first = wrap(PARA_1, 20, 20, 240)
    second = wrap(PARA_2, 20, 20 + 20 * len(first), 240)
    blocks = segment_lines([*first, *second])
    assert len(first) >= 3 and len(blocks) == 1
    expected = [False] + [True] * (len(first) - 1) + [False] + [True] * (len(second) - 1)
    assert [soft for _text, soft in shape(blocks)[0]] == expected


CODE = [
    "def main():",
    "    cfg = load_config(path)",
    "    if cfg.debug:",
    "        print(cfg)",
    "    return run(cfg)",
]


def test_code_lines_in_a_monospace_block_and_menu_items_stay_hard() -> None:
    code = [span(20, 100 + 18 * i, text, mono=True) for i, text in enumerate(CODE)]
    menu = [span(400, 100 + 40 * i, item) for i, item in enumerate(["Settings", "History", "About"])]  # 行距宽但均匀
    blocks = segment_lines([*code, *menu])
    assert shape(blocks) == [
        [(text.strip(), False) for text in CODE],  # 最长行后跟同缩进行：等宽块整块硬换行
        [("Settings", False), ("History", False), ("About", False)],
    ]


def test_a_blank_line_between_paragraphs_starts_a_new_block() -> None:
    first = wrap(PARA_1, 20, 20, 240)
    second = wrap(PARA_2, 20, 20 + 20 * (len(first) + 1), 240)  # 段间空一行
    blocks = segment_lines([*first, *second])
    assert [[line.text for line in block] for block in blocks] == [[l.text for l in first], [l.text for l in second]]


def test_hanging_indent_continuation_is_soft_and_next_bullet_is_hard() -> None:
    first = span(20, 20, "1. Download the release archive from the project")
    blocks = segment_lines([first, span(first.words[1][0], 40, "page and unpack it anywhere."), span(20, 60, "2. Run it.")])
    assert shape(blocks) == [[
        ("1. Download the release archive from the project", False),
        ("page and unpack it anywhere.", True),
        ("2. Run it.", False),
    ]]


def test_two_columns_and_table_columns_are_separate_blocks() -> None:
    left = wrap("Left column text wraps inside its own width only.", 20, 20, 200)
    right = wrap("Right column text wraps inside its own width too.", 400, 30, 200)  # 两栏行不对齐
    table = [span(20, 120, "Name"), span(200, 120, "Size"), span(20, 140, "model.bin"), span(200, 140, "4.4 GB")]
    blocks = segment_lines([*left, *right, *table])
    assert [[line.text for line in block] for block in blocks] == [
        [line.text for line in left],
        [line.text for line in right],
        ["Name", "model.bin"],
        ["Size", "4.4 GB"],
    ]
    assert [line.soft_wrap for line in blocks[0]][1:] == [True] * (len(left) - 1)


def test_heading_with_a_different_font_size_starts_its_own_block() -> None:
    heading = LineSpan(20, 10, 200, 34, "Getting started", ((20, 98, "Getting"), (110, 200, "started")))
    body = wrap("Install the package and run the setup wizard to finish the installation.", 20, 42, 300)
    blocks = segment_lines([heading, *body])
    assert [[line.text for line in block] for block in blocks] == [["Getting started"], [line.text for line in body]]


def test_chinese_wrap_needs_twelve_characters_and_joins_at_the_right_edge() -> None:
    blocks = segment_lines([
        span(20, 20, "这是一段很长的中文说明文字它会在右边缘自动折行"),
        span(20, 42, "接着显示下一行"),
        span(20, 64, "短行标题"),  # 上一行没顶到右缘：首字放得进
        span(400, 20, "设置选项"),  # 另一列：上一行就是右缘，但不足 12 个中文字
        span(400, 42, "帮助文档"),
    ])
    assert shape(blocks) == [
        [("这是一段很长的中文说明文字它会在右边缘自动折行", False), ("接着显示下一行", True), ("短行标题", False)],
        [("设置选项", False), ("帮助文档", False)],
    ]
