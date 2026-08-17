# -*- coding: utf-8 -*-
"""去識別化把關工具。

本專案的 GitHub repo 為公開，因此**任何進版控的內容都不得含真實人名、
真實廠商名、真實工地名或公司內部敏感字詞**。

設計重點：對照表本身就是還原金鑰，因此它不進版控。
  - deid_rules.local.json   真實 ↔ 代稱對照表（.gitignore 排除，只存在本機）
  - deid_rules.example.json 結構範例（進版控，內容全為假資料）

用法：
    python tools/deidentify.py --check              # 掃描 git 追蹤中的所有檔案
    python tools/deidentify.py --check --staged     # 只掃描已 staged 的檔案（給 hook 用）
    python tools/deidentify.py --check path1 path2  # 掃描指定檔案
    python tools/deidentify.py --apply <檔案>        # 真實 → 代稱（就地改寫）
    python tools/deidentify.py --restore <檔案> [-o 輸出]
                                                    # 代稱 → 真實，產生對內用版本
    python tools/deidentify.py --rules              # 顯示規則統計（不顯示內容）

退出碼：0 = 乾淨；1 = 發現真實字詞（pre-commit hook 會據此擋下 commit）
"""
import argparse
import json
import os
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RULES_LOCAL = os.path.join(BASE_DIR, "deid_rules.local.json")
RULES_EXAMPLE = os.path.join(BASE_DIR, "deid_rules.example.json")

# 不掃描的路徑（二進位、產出物、工具自身）
SKIP_PREFIXES = ("uploads/", ".git/", "__pycache__/")
SKIP_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".pdf", ".db", ".ico", ".zip")
SKIP_FILES = {"tools/deidentify.py", "deid_rules.local.json", "DEIDENTIFICATION.md"}


def load_rules():
    path = RULES_LOCAL if os.path.exists(RULES_LOCAL) else None
    if path is None:
        print("[去識別化] 找不到 deid_rules.local.json。", file=sys.stderr)
        print("           請複製 deid_rules.example.json 為 deid_rules.local.json，"
              "填入真實對照後再試。", file=sys.stderr)
        print("           該檔已列入 .gitignore，不會被推上 GitHub。", file=sys.stderr)
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    terms = data.get("terms", [])
    for t in terms:
        if not t.get("real") or not t.get("public"):
            raise SystemExit(f"[去識別化] 規則格式錯誤，缺 real 或 public：{t}")
    return terms


def tracked_files(staged: bool):
    cmd = (["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"]
           if staged else ["git", "ls-files"])
    out = subprocess.run(cmd, cwd=BASE_DIR, capture_output=True, text=True,
                         encoding="utf-8")
    return [p for p in out.stdout.splitlines() if p.strip()]


def should_skip(rel: str) -> bool:
    rel = rel.replace("\\", "/")
    if rel in SKIP_FILES:
        return True
    if rel.startswith(SKIP_PREFIXES):
        return True
    return rel.lower().endswith(SKIP_SUFFIXES)


def read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
        return None


def check(paths, terms) -> int:
    hits = []
    for rel in paths:
        if should_skip(rel):
            continue
        full = os.path.join(BASE_DIR, rel)
        content = read_text(full)
        if content is None:
            continue
        for lineno, line in enumerate(content.splitlines(), 1):
            for t in terms:
                if t["real"] in line:
                    hits.append((rel, lineno, t["real"], t["public"],
                                 t.get("type", "-")))

    if not hits:
        print(f"[去識別化] 通過：掃描 {len(paths)} 個檔案，未發現真實字詞。")
        return 0

    print("\n[去識別化] 不通過 —— 以下位置含真實字詞，不得推上公開 repo：\n",
          file=sys.stderr)
    for rel, lineno, real, public, typ in hits:
        print(f"  {rel}:{lineno}  [{typ}] 「{real}」 → 請改為 「{public}」",
              file=sys.stderr)
    print(f"\n共 {len(hits)} 處。可執行下列指令自動置換：", file=sys.stderr)
    for rel in sorted({h[0] for h in hits}):
        print(f"  python tools/deidentify.py --apply {rel}", file=sys.stderr)
    print("", file=sys.stderr)
    return 1


def transform(path, terms, reverse=False, out_path=None) -> int:
    full = path if os.path.isabs(path) else os.path.join(BASE_DIR, path)
    content = read_text(full)
    if content is None:
        raise SystemExit(f"[去識別化] 無法讀取 {path}")
    n = 0
    # 長字串優先置換，避免短字串先命中造成部分replace
    ordered = sorted(terms, key=lambda t: -len(t["real" if not reverse else "public"]))
    for t in ordered:
        src, dst = (t["public"], t["real"]) if reverse else (t["real"], t["public"])
        if src in content:
            n += content.count(src)
            content = content.replace(src, dst)
    target = out_path or full
    with open(target, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    action = "還原" if reverse else "去識別化"
    print(f"[去識別化] {action}完成：{path} 置換 {n} 處 → {os.path.relpath(target, BASE_DIR)}")
    return n


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--check", action="store_true", help="掃描是否含真實字詞")
    ap.add_argument("--staged", action="store_true", help="只掃描已 staged 的檔案")
    ap.add_argument("--apply", metavar="FILE", help="真實 → 代稱，就地改寫")
    ap.add_argument("--restore", metavar="FILE", help="代稱 → 真實，產生對內版本")
    ap.add_argument("-o", "--output", help="--restore 的輸出路徑")
    ap.add_argument("--rules", action="store_true", help="顯示規則統計")
    ap.add_argument("paths", nargs="*", help="要掃描的檔案（預設為全部追蹤檔案）")
    args = ap.parse_args()

    terms = load_rules()
    if terms is None:
        # 找不到規則檔時不擋 commit，但明確警告；避免新環境無法運作
        return 0 if not args.check else 0

    if args.rules:
        by_type = {}
        for t in terms:
            by_type[t.get("type", "-")] = by_type.get(t.get("type", "-"), 0) + 1
        print(f"[去識別化] 共 {len(terms)} 條規則：")
        for k, v in sorted(by_type.items()):
            print(f"    {k:<10} {v} 條")
        return 0

    if args.apply:
        transform(args.apply, terms)
        return 0

    if args.restore:
        out = args.output or (args.restore + ".internal")
        transform(args.restore, terms, reverse=True, out_path=out)
        return 0

    if args.check:
        paths = args.paths or tracked_files(args.staged)
        return check(paths, terms)

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
