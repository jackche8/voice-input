"""T12 真实 tesseract 识别精度：生成器样本逐行对照真值（行框、文字、分块、软/硬换行、代码缩进），全屏不超时。

缺 tesseract、默认语言包或样本字体时按 tests/logic/test_ocr.py 的惯例 skip（本机须实跑）。
"""
from __future__ import annotations

import difflib
import re

import pytest

from _ocr import DEFAULT_OCR_LANG, clear_language_cache
from _ocr_layout import extract_ocr_layout
from tests.logic.ocr_samples import (
    ACCURACY_SAMPLES,
    BBOX_TOLERANCE_PX,
    FULLSCREEN_SAMPLES,
    all_samples,
    bbox_close,
    fonts_missing,
    text_similar,
)

# 已知限制（方案第七节）：简繁两个语言包同时加载时，Tesseract 会把个别繁体短行整行按简体识别（字义不变，
# 实测「個過程不需要手動干預。」→「个过程不需要手动干预。」，调换语言顺序也一样）。繁体段落样本因此只要求每行
# 至少一半汉字与真值一致（简繁同形字足以区分「认对了这一行」与串行、漏行）；行框、分块、软/硬换行与行数照常严格
SCRIPT_MIXED_SAMPLES = frozenset({"traditional_paragraph"})
_HANZI = re.compile("[\u3400-\u4dbf\u4e00-\u9fff]")


def _half_hanzi_match(found: str, truth: str) -> bool:
    a, b = "".join(_HANZI.findall(found)), "".join(_HANZI.findall(truth))
    matched = sum(block.size for block in difflib.SequenceMatcher(None, a, b).get_matching_blocks())
    return 2 * matched >= len(b)


@pytest.fixture(scope="module", autouse=True)
def _real_tesseract():
    pytesseract = pytest.importorskip("pytesseract")
    clear_language_cache()
    try:
        langs = set(pytesseract.get_languages(config=""))
    except Exception as exc:
        pytest.skip(f"无法查询 tesseract 语言包: {exc}")
    missing = [lang for lang in DEFAULT_OCR_LANG.split("+") if lang not in langs]
    if missing:
        pytest.skip(f"本机未安装 tesseract 语言包: {missing}")
    reason = fonts_missing()
    if reason:
        pytest.skip(reason)


@pytest.mark.parametrize("name", ACCURACY_SAMPLES)
def test_every_truth_line_is_recognized_in_place_with_blocks_and_wraps(name: str) -> None:
    sample = all_samples()[name]
    _w, _h, _text, blocks = extract_ocr_layout(sample.png)
    found = [(index, line) for index, block in enumerate(blocks) for line in block.lines]
    assert len(found) == len(sample.lines), f"识别出 {len(found)} 行，真值 {len(sample.lines)} 行"
    block_of: dict[int, int] = {}
    for truth in sample.lines:
        hits = [(index, line) for index, line in found if bbox_close(line.bbox, truth.bbox)]
        assert hits, f"{truth.text!r} 没有行框偏差 ≤{BBOX_TOLERANCE_PX}px 的识别行（真值 {truth.bbox}）"
        index, line = hits[0]
        same_text = _half_hanzi_match if name in SCRIPT_MIXED_SAMPLES else text_similar
        assert same_text(line.text, truth.text), (truth.text, line.text)
        assert line.soft_wrap == truth.soft_wrap, (truth.text, truth.soft_wrap)
        assert block_of.setdefault(truth.block, index) == index, f"{truth.text!r} 被分到了别的块"
    assert len(set(block_of.values())) == len(block_of), "不同真值块被合成了一块"
    if name in ("code", "terminal"):  # 复制代码：逐行、无多余空行、缩进保留
        copied = blocks[0].ocr_text.split("\n")
        assert [len(row) - len(row.lstrip(" ")) for row in copied] == [t.indent for t in sample.lines]


@pytest.mark.parametrize("name", FULLSCREEN_SAMPLES)
def test_fullscreen_selection_finishes_within_the_ocr_timeout(name: str) -> None:
    sample = all_samples()[name]
    _w, _h, text, blocks = extract_ocr_layout(sample.png)  # 超时会抛 LogicError(timeout)
    assert blocks and text
