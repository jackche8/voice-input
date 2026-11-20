"""T10 文本派生：一份行数据派生显示原文、硬行段、块译文与全文（_ocr_text 唯一实现）。"""
from __future__ import annotations

import pytest

from _contracts import ErrorCode, LogicError, OcrTextLine
from _ocr_text import HardSegment, block_display_text, block_segments, block_translated_text, join_block_texts


def line(x: int, y: int, text: str, *, soft: bool = False, char_w: int = 8) -> OcrTextLine:
    return OcrTextLine(bbox=(x, y, char_w * len(text), 16), text=text, soft_wrap=soft)


CODE = [
    line(20, 100, "def main():"),
    line(52, 118, "cfg = load_config(path)"),  # 缩进 4 个字宽
    line(52, 136, "return run(cfg)  # 运行"),
]
PARAGRAPH = [
    line(20, 20, "The quick installer downloads the model"),
    line(20, 40, "weights and verifies every checksum.", soft=True),
    line(20, 60, "Restart the daemon afterwards."),
]
CHINESE = [line(20, 20, "这是第一行中文", char_w=16), line(20, 40, "接着的第二行", soft=True, char_w=16)]


def test_display_text_keeps_every_visible_line_and_indent() -> None:
    assert block_display_text(CODE) == "def main():\n    cfg = load_config(path)\n    return run(cfg)  # 运行"
    assert block_display_text(PARAGRAPH).count("\n") == 2  # 软换行也是一条可见行


def test_segments_join_soft_wraps_without_space_at_cjk_seams() -> None:
    assert block_segments(PARAGRAPH) == [
        HardSegment(0, 0, "The quick installer downloads the model weights and verifies every checksum."),
        HardSegment(2, 0, "Restart the daemon afterwards."),
    ]
    assert [s.text for s in block_segments(CHINESE)] == ["这是第一行中文接着的第二行"]
    assert [(s.first_line, s.indent) for s in block_segments(CODE)] == [(0, 0), (1, 4), (2, 4)]


def test_block_translation_restores_segment_indents_and_rejects_count_mismatch() -> None:
    translated = block_translated_text(CODE, ["def main():", "cfg = load_config(path)", "return run(cfg)  # run"])
    assert translated == "def main():\n    cfg = load_config(path)\n    return run(cfg)  # run"
    assert block_translated_text(PARAGRAPH, ["第一段。", "第二段。"]) == "第一段。\n第二段。"
    with pytest.raises(LogicError) as err:
        block_translated_text(PARAGRAPH, ["只有一条"])
    assert err.value.code == ErrorCode.translate_failed


def test_full_text_separates_non_empty_blocks_with_one_blank_line() -> None:
    assert join_block_texts(["a\n  b", "", "  ", "c"]) == "a\n  b\n\nc"
    assert join_block_texts([]) == ""
