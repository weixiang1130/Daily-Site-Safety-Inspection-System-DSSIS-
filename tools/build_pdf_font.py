"""把 PDF 用的中文字型從 OTF（CFF 輪廓）轉成 TTF（glyf 輪廓）。

為什麼需要這一步
----------------
Noto Sans TC 的 OTF 是 CID-keyed CFF，內含 18 組 FDArray。pdf-lib 產生
PDF 時會呼叫 fontkit 做子集化，而 fontkit 的 CFF 子集化在處理 CID-keyed
字型時會弄壞 FDSelect／FDArray 的對應：落在 FD index 0 的字（拉丁字母、
數字）還畫得出來，其餘 FD index 的字（也就是所有中文）會變成空白。

症狀就是產出的 PDF「只有英數字，中文全空」。

fontkit 對 TrueType（glyf）輪廓的子集化沒有這個問題，因此改為預先把輪廓
轉成二次貝茲曲線。轉換是一次性的，產出的 TTF 進版控，執行期仍然照常做
子集化，所以單份 PDF 不會變大。

用法
----
    python tools/build_pdf_font.py

輸出 backend/cloud/assets/fonts/NotoSansTC-Regular.ttf。
授權沿用同目錄的 LICENSE.txt（SIL OFL 1.1），轉換不改變授權條件。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from fontTools.ttLib import TTFont, newTable
from fontTools.pens.cu2quPen import Cu2QuPen
from fontTools.pens.ttGlyphPen import TTGlyphPen

FONT_DIR = Path(__file__).resolve().parent.parent / "backend/cloud/assets/fonts"
SRC = FONT_DIR / "NotoSansTC-Regular.otf"
DST = FONT_DIR / "NotoSansTC-Regular.ttf"

# 曲線轉換容許誤差（字型單位）。upem 為 1000 時，1.0 對於內文尺寸完全看不出來。
MAX_ERR = 1.0

# CFF 專屬、轉成 glyf 後不再有意義的表。DSIG 是數位簽章，輪廓改了就失效。
DROP_TABLES = ["CFF ", "VORG", "DSIG"]


def convert() -> None:
    if not SRC.exists():
        sys.exit(f"找不到來源字型：{SRC}")

    print(f"讀取 {SRC.name}（{SRC.stat().st_size / 1e6:.1f} MB）")
    font = TTFont(SRC)
    glyph_set = font.getGlyphSet()
    names = font.getGlyphOrder()
    print(f"字數 {len(names)}，開始轉換輪廓（這一步需要數分鐘）")

    glyf = newTable("glyf")
    glyf.glyphOrder = names
    glyf.glyphs = {}

    start = time.time()
    for i, name in enumerate(names):
        pen = TTGlyphPen(glyph_set)
        # reverse_direction：TrueType 的外框繞行方向與 PostScript 相反
        glyph_set[name].draw(Cu2QuPen(pen, MAX_ERR, reverse_direction=True))
        glyf.glyphs[name] = pen.glyph()

        if i and i % 5000 == 0:
            print(f"  {i}/{len(names)}（{time.time() - start:.0f} 秒）")

    font["glyf"] = glyf
    # loca 是 glyf 的位移索引表，內容由 glyf 在存檔階段填入，
    # 但表本身必須先存在，否則寫出的檔案缺 loca 而無法讀回。
    font["loca"] = newTable("loca")

    # maxp 從 CFF 的 version 0.5 升為 TrueType 的 1.0，並依實際輪廓重算統計值
    font["maxp"].tableVersion = 0x00010000
    for attr, value in (
        ("maxZones", 1), ("maxTwilightPoints", 0), ("maxStorage", 0),
        ("maxFunctionDefs", 0), ("maxInstructionDefs", 0),
        ("maxStackElements", 0), ("maxSizeOfInstructions", 0),
        ("maxComponentElements", 0),
    ):
        setattr(font["maxp"], attr, value)

    # 沒有 hinting 指令，這兩個欄位必須明確歸零
    font["head"].glyphDataFormat = 0
    font["head"].indexToLocFormat = 0     # 實際值由 compile 階段依大小自行調整

    # post 用 format 3.0：不保留字符名稱表，可省下可觀的體積，
    # PDF 嵌入不需要字符名稱。
    font["post"].formatType = 3.0

    for tag in DROP_TABLES:
        if tag in font:
            del font[tag]

    font.sfntVersion = "\x00\x01\x00\x00"   # 標記為 TrueType 而非 OTTO

    print(f"寫出 {DST.name}")
    font.save(DST)
    verify()


def verify() -> None:
    """確認中文字真的有輪廓——這正是原本壞掉的地方，不能只看檔案存在。"""
    font = TTFont(DST)
    assert font.sfntVersion == "\x00\x01\x00\x00", "sfntVersion 仍不是 TrueType"
    assert "glyf" in font and "CFF " not in font, "glyf／CFF 表狀態不正確"

    cmap = font.getBestCmap()
    glyf = font["glyf"]
    samples = "職安全衛生缺失改善廠商巡檢工地日報表現場"
    missing = [c for c in samples if ord(c) not in cmap]
    assert not missing, f"字型缺字：{''.join(missing)}"

    empty = []
    for ch in samples:
        g = glyf[cmap[ord(ch)]]
        if g.numberOfContours == 0:
            empty.append(ch)
    assert not empty, f"下列中文字沒有輪廓：{''.join(empty)}"

    size = DST.stat().st_size / 1e6
    print(f"檢查通過：{len(samples)} 個中文字皆有輪廓，檔案 {size:.1f} MB")


if __name__ == "__main__":
    convert()
