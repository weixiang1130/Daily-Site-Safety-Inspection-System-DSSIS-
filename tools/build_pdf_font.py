"""建置 PDF 用的中文字型。

為什麼需要這一步
----------------
pdf-lib 產生 PDF 時會呼叫 fontkit 做字型子集化，而 fontkit 的子集化對
Noto Sans TC 是壞的——實測（2026-08-19，pdf-lib 1.17.1 + @pdf-lib/fontkit
1.1.1，以 pdf.js 渲染後數出實際畫出的字）：

    字型格式          子集化   21 個字裡畫出
    ---------------   ------   ------------
    原始 OTF          是        13      ← 先前線上就是這組，中文變空白
    原始 OTF          否         9
    轉檔後 TTF        是         3
    轉檔後 TTF        否        21      ← 唯一正確
    精簡後 TTF        是        11
    精簡後 TTF        否        21      ← 本工具採用

結論是**只要交給 fontkit 子集化就會掉字**，與字型格式、字型大小都無關。
因此改為：建置階段先把字型縮到系統需要的字，執行期以 `subset: false`
整份嵌入，完全不讓 fontkit 碰子集化。

（順帶一提，原始 OTF 是 CID-keyed CFF，即使不子集化 pdf-lib 也畫不對，
所以輪廓必須先轉成 TrueType 的 glyf 格式。）

收錄哪些字
----------
1. ASCII 與常用標點
2. 系統本身會印出的字：檢查表項目、程式碼裡的標籤
3. Big5 常用字（5401 字）——涵蓋現場自由輸入的絕大多數內容

未收錄的字不會靜默消失：`backend/cloud/lib/font-coverage.ts` 會一併產生，
PDF 產出時遇到收錄範圍外的字會畫成 □ 並在記錄檔留下警告，方便日後依實際
資料擴充，而不是讓安全紀錄少一個字還沒人發現。

用法
----
    python tools/build_pdf_font.py

產出
    backend/cloud/assets/fonts/NotoSansTC-Regular.ttf   （約 1.7 MB）
    backend/cloud/lib/font-coverage.ts
授權沿用同目錄的 LICENSE.txt（SIL OFL 1.1），轉換與精簡不改變授權條件。
"""

from __future__ import annotations

import glob
import io
import os
import sys
import time
from pathlib import Path

from fontTools import subset
from fontTools.pens.cu2quPen import Cu2QuPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont, newTable

ROOT = Path(__file__).resolve().parent.parent
FONT_DIR = ROOT / "backend/cloud/assets/fonts"
SRC = FONT_DIR / "NotoSansTC-Regular.otf"
DST = FONT_DIR / "NotoSansTC-Regular.ttf"
COVERAGE_TS = ROOT / "backend/cloud/lib/font-coverage.ts"

# 曲線轉換容許誤差（字型單位）。upem 為 1000 時，1.0 在內文尺寸看不出來。
MAX_ERR = 1.0

# CFF 專屬、轉成 glyf 後不再有意義的表。DSIG 是數位簽章，輪廓改了就失效。
DROP_TABLES = ["CFF ", "VORG", "DSIG"]

PUNCT = "　、。，；：？！（）「」『』【】《》〈〉—…～·※°µμ³²±§％＃＠"

# 系統文字的來源：檢查表題庫、後端會印出的標籤、前端頁面上的字
TEXT_SOURCES = ["data/forms.json", "backend/cloud/lib/*.ts", "frontend/*.html"]


def repertoire() -> set[str]:
    """決定要收錄哪些字。"""
    chars = {chr(c) for c in range(0x20, 0x7F)} | set(PUNCT)

    n_before = len(chars)
    for pattern in TEXT_SOURCES:
        for path in glob.glob(str(ROOT / pattern)):
            chars |= set(io.open(path, encoding="utf-8").read())
    print(f"系統文字帶入 {len(chars) - n_before} 個字元")

    # Big5 常用字區（0xA440–0xC67E）。以編碼區間推導而非外部清單，
    # 好讓這支工具離線、可重現。
    n_before = len(chars)
    for hi in range(0xA4, 0xC7):
        for lo in list(range(0x40, 0x7F)) + list(range(0xA1, 0xFF)):
            try:
                chars.add(bytes([hi, lo]).decode("big5"))
            except UnicodeDecodeError:
                pass
    print(f"Big5 常用字帶入 {len(chars) - n_before} 個字元")

    return chars


def to_truetype(font: TTFont) -> None:
    """把 CFF 的三次貝茲輪廓改寫成 TrueType 的二次貝茲輪廓（就地修改）。"""
    glyph_set = font.getGlyphSet()
    names = font.getGlyphOrder()
    print(f"轉換輪廓：{len(names)} 個字符（需要數分鐘）")

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

    # maxp 從 CFF 的 0.5 升為 TrueType 的 1.0；沒有 hinting，統計值全部歸零
    font["maxp"].tableVersion = 0x00010000
    for attr, value in (
        ("maxZones", 1), ("maxTwilightPoints", 0), ("maxStorage", 0),
        ("maxFunctionDefs", 0), ("maxInstructionDefs", 0),
        ("maxStackElements", 0), ("maxSizeOfInstructions", 0),
        ("maxComponentElements", 0),
    ):
        setattr(font["maxp"], attr, value)

    font["head"].glyphDataFormat = 0
    font["head"].indexToLocFormat = 0     # 實際值由存檔階段依大小自行調整
    font["post"].formatType = 3.0         # 不保留字符名稱，PDF 嵌入用不到

    for tag in DROP_TABLES:
        if tag in font:
            del font[tag]

    font.sfntVersion = "\x00\x01\x00\x00"   # 標記為 TrueType 而非 OTTO


def write_coverage(font: TTFont) -> None:
    """把字型實際涵蓋的碼位寫成 TypeScript，供產 PDF 時檢查缺字。"""
    points = sorted(font.getBestCmap())

    ranges: list[tuple[int, int]] = []
    for cp in points:
        if ranges and cp == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], cp)
        else:
            ranges.append((cp, cp))

    body = ",\n  ".join(f"[{a}, {b}]" for a, b in ranges)
    COVERAGE_TS.write_text(
        "// 由 tools/build_pdf_font.py 自動產生，請勿手動編輯。\n"
        "//\n"
        "// PDF 用的字型是建置階段就縮好的（原因見該工具說明），因此收錄範圍外\n"
        "// 的字畫不出來。這份碼位清單讓 pdf.ts 能在產出前就發現缺字，改畫成 □\n"
        "// 並留下警告，而不是讓安全紀錄靜默少一個字。\n"
        "//\n"
        f"// 涵蓋 {len(points)} 個碼位、{len(ranges)} 個區間。\n"
        "export const FONT_COVERAGE: ReadonlyArray<readonly [number, number]> = [\n"
        f"  {body},\n];\n",
        encoding="utf-8",
    )
    print(f"寫出 {COVERAGE_TS.name}：{len(points)} 個碼位、{len(ranges)} 個區間")


def verify() -> None:
    """確認中文字真的有輪廓——這正是原本壞掉的地方，不能只看檔案存在。"""
    font = TTFont(DST)
    assert font.sfntVersion == "\x00\x01\x00\x00", "sfntVersion 不是 TrueType"
    assert "glyf" in font and "CFF " not in font, "glyf／CFF 表狀態不正確"

    cmap, glyf = font.getBestCmap(), font["glyf"]
    samples = "職安全衛生缺失改善廠商巡檢工地日報表現場ABC123"
    missing = [c for c in samples if ord(c) not in cmap]
    assert not missing, f"字型缺字：{''.join(missing)}"

    empty = [c for c in samples
             if glyf[cmap[ord(c)]].numberOfContours == 0 and not c.isspace()]
    assert not empty, f"下列字沒有輪廓：{''.join(empty)}"

    print(f"檢查通過：抽驗 {len(samples)} 字皆有輪廓，"
          f"檔案 {DST.stat().st_size / 1e6:.2f} MB")


def main() -> None:
    if not SRC.exists():
        sys.exit(f"找不到來源字型：{SRC}")

    chars = repertoire()
    print(f"共收錄 {len(chars)} 個字元")

    print(f"讀取 {SRC.name}（{SRC.stat().st_size / 1e6:.1f} MB）")
    font = TTFont(SRC)
    to_truetype(font)

    print("精簡字型")
    # 版面配置特性（連字、替代字形等）在此用不到，留著只是佔體積
    sub = subset.Subsetter(options=subset.Options(
        notdef_outline=True, layout_features=[]))
    sub.populate(text="".join(sorted(chars)))
    sub.subset(font)

    font.save(DST)
    write_coverage(TTFont(DST))
    verify()


if __name__ == "__main__":
    main()
