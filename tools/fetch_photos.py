#!/usr/bin/env python3
"""为单词表里的词找「真实照片」，人工挑选后生成最终图片并写回 words.json。

工作流（三步）：
  1) 抓候选：python3 tools/fetch_photos.py --fetch --only apple,banana
     - 从 Wikimedia Commons 搜每个词的候选图（白底/孤图优先）
     - 缩略图存到 photos/_candidates/<slug>/，同时生成拼图 photos/_review/sheet-*.png
  2) 人工挑选：看拼图，把选中的候选编号写进 photos/picks.json，例如 {"apple": [2], "banana": [0]}
     - 值为候选编号列表；写 -1 或 [] 表示这个词暂时不用照片（保留图标）
  3) 落地：python3 tools/fetch_photos.py --finalize
     - 用 900px 版本下载选中的图到 photos/<slug>.jpg
     - 写 words.json 的 photo 字段、photos/ATTRIBUTION.md

常用参数：--words-out、--candidates 张数、--jobs 并发、--force 重抓、--sheet-cols。
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WORDS = ROOT / "words.json"
PHOTOS = ROOT / "photos"
CAND = PHOTOS / "_candidates"
REVIEW = PHOTOS / "_review"
PICKS = PHOTOS / "picks.json"
ATTRIB = PHOTOS / "ATTRIBUTION.md"
API = "https://commons.wikimedia.org/w/api.php"
UA = "vocab-test-photo-fetcher/1.0 (personal vocabulary cards)"

BAD_TITLE = re.compile(
    r"(logo|icon|diagram|chart|map of|painting|drawing|engraving|illustration"
    r"|cartoon|statue|sculpture|monument|poster|sign|text|font|screenshot"
    r"|coat of arms|flag of|seal of|stamp|coin|museum label)",
    re.I,
)
GOOD_TITLE = re.compile(r"(white background|isolated|cut ?out|on white)", re.I)


# ---------------------------------------------------------------- 基础工具
def curl_json(url: str, retries: int = 3) -> dict:
    cmd = ["curl", "-sL", "-m", "40", "-A", UA, "-H", "Accept: application/json", url]
    last = ""
    for attempt in range(retries):
        r = subprocess.run(cmd, capture_output=True, text=True)
        last = r.stdout
        try:
            return json.loads(last)
        except Exception:
            time.sleep(1.2 * (attempt + 1))
    print(f"  ! API 失败: {url[:120]}", file=sys.stderr)
    return {}


def curl_file(url: str, dest: Path, retries: int = 3) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["curl", "-sL", "-m", "90", "-A", UA, "-o", str(dest), "-w", "%{http_code}", url]
    for attempt in range(retries):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True)
            code = r.stdout.strip()
            if code == "200" and dest.exists() and dest.stat().st_size > 3000:
                return True
        except Exception:
            pass
        time.sleep(1.0 * (attempt + 1))
    if dest.exists() and dest.stat().st_size <= 3000:
        dest.unlink(missing_ok=True)
    return False


def slugify(text: str) -> str:
    s = (text or "").strip().lower()
    s = s.replace("'", "").replace("’", "")
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "word"


def clean(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", html.unescape(s or ""))
    return re.sub(r"\s+", " ", s).strip()


def short(s: str, n: int = 46) -> str:
    s = clean(s)
    return s if len(s) <= n else s[: n - 1] + "…"


# ---------------------------------------------------------------- 候选抓取
def search_commons(query: str, limit: int = 12) -> list[dict]:
    import urllib.parse

    params = {
        "action": "query",
        "generator": "search",
        "gsrsearch": query,
        "gsrnamespace": "6",
        "gsrlimit": str(limit),
        "prop": "imageinfo",
        "iiprop": "url|extmetadata|mime|size",
        "iiurlwidth": "520",
        "format": "json",
        "formatversion": "2",
    }
    url = API + "?" + urllib.parse.urlencode(params)
    data = curl_json(url)
    out = []
    for page in data.get("query", {}).get("pages", []) or []:
        infos = page.get("imageinfo") or []
        if not infos:
            continue
        info = infos[0]
        mime = (info.get("mime") or "").lower()
        if mime not in ("image/jpeg", "image/png", "image/webp"):
            continue
        meta = info.get("extmetadata") or {}
        thumb = info.get("thumburl") or info.get("url")
        if not thumb:
            continue
        out.append(
            {
                "title": page.get("title", ""),
                "thumb": thumb,
                "width900": info.get("url", ""),
                "page": info.get("descriptionurl", ""),
                "license": clean((meta.get("LicenseShortName") or {}).get("value", "")),
                "artist": short((meta.get("Artist") or {}).get("value", ""), 70),
                "w": info.get("width", 0),
                "h": info.get("height", 0),
            }
        )
    return out


def score_candidate(cand: dict, word: str) -> float:
    title = cand["title"].lower()
    score = 0.0
    if slugify(word) in slugify(title):
        score += 3
    if GOOD_TITLE.search(title):
        score += 4
    if BAD_TITLE.search(title):
        score -= 8
    if title.endswith(".png"):
        score += 1
    if cand["w"] and cand["h"] and min(cand["w"], cand["h"]) >= 500:
        score += 2
    if cand["w"] and cand["h"] and cand["w"] / max(cand["h"], 1) > 2.2:
        score -= 2
    return score


def fetch_word(word: str, args) -> tuple[str, int]:
    slug = slugify(word)
    folder = CAND / slug
    meta_path = folder / "meta.json"
    if meta_path.exists() and not args.force:
        try:
            meta = json.load(open(meta_path))
            if meta:
                return word, len(meta)
        except Exception:
            pass
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("*.jpg"):
        old.unlink()
    seen: set[str] = set()
    pool: list[dict] = []
    queries = [f"{word} white background", f"{word} isolated", f"{word} photo"]
    for q in queries:
        if len(pool) >= args.candidates * 2:
            break
        for cand in search_commons(q):
            key = cand["title"].lower()
            if key in seen:
                continue
            seen.add(key)
            pool.append(cand)
        time.sleep(0.5)
    pool.sort(key=lambda c: score_candidate(c, word), reverse=True)
    picked: list[dict] = []
    for cand in pool:
        if len(picked) >= args.candidates:
            break
        idx = len(picked)
        dest = folder / f"{idx:02d}.jpg"
        if curl_file(cand["thumb"], dest):
            cand["file"] = f"{idx:02d}.jpg"
            cand["score"] = round(score_candidate(cand, word), 1)
            picked.append(cand)
        time.sleep(0.1)
    json.dump(picked, open(meta_path, "w"), ensure_ascii=False, indent=1)
    return word, len(picked)


# ---------------------------------------------------------------- 拼图
def build_sheets(words: list[str], cols: int = 4) -> list[Path]:
    from PIL import Image, ImageDraw, ImageFont

    try:
        font = ImageFont.truetype("/System/Library/Fonts/PingFang.ttc", 15)
        font_small = ImageFont.truetype("/System/Library/Fonts/PingFang.ttc", 12)
    except Exception:
        font = font_small = ImageFont.load_default()

    REVIEW.mkdir(parents=True, exist_ok=True)
    CELL = 214
    PAD = 10
    LABEL = 30
    sheets: list[Path] = []
    per_sheet = 8  # 8 词一页
    for page, start in enumerate(range(0, len(words), per_sheet)):
        chunk = words[start : start + per_sheet]
        rows = len(chunk)
        W = cols * (CELL + PAD) + PAD
        H = rows * (CELL + LABEL + PAD) + PAD
        sheet = Image.new("RGB", (W, H), "white")
        dr = ImageDraw.Draw(sheet)
        for r, word in enumerate(chunk):
            y = PAD + r * (CELL + LABEL + PAD)
            dr.text((PAD, y + 4), word, fill="#222", font=font)
            meta = json.load(open(CAND / slugify(word) / "meta.json")) if (CAND / slugify(word) / "meta.json").exists() else []
            for c in range(cols):
                x = PAD + c * (CELL + PAD)
                if c < len(meta):
                    f = CAND / slugify(word) / meta[c]["file"]
                    try:
                        im = Image.open(f).convert("RGB")
                        im.thumbnail((CELL, CELL - 24))
                        sheet.paste(im, (x + (CELL - im.width) // 2, y + LABEL + 24 + (CELL - 24 - im.height) // 2))
                    except Exception:
                        pass
                    label = f"[{c}] {meta[c].get('license','?')} {short(meta[c]['title'].replace('File:', ''), 40)}"
                    dr.text((x + 2, y + LABEL + 2), label, fill="#5a5a5a", font=font_small)
                dr.rectangle([x, y + LABEL + 24, x + CELL, y + LABEL + CELL], outline=(230, 230, 230))
        out = REVIEW / f"sheet-{page + 1}.png"
        sheet.save(out)
        sheets.append(out)
    return sheets


# ---------------------------------------------------------------- 落地
def finalize(args) -> None:
    words_data = json.load(open(args.words_out))
    picks = json.load(open(PICKS)) if PICKS.exists() else {}
    by_slug = {slugify(w["word"]): w for w in words_data["words"]}
    made = 0
    lines = [
        "# 照片来源与署名",
        "",
        "本目录照片来自 Wikimedia Commons，均为自由许可（CC0 / 公有领域 / CC BY / CC BY-SA 等）。",
        "仅用于家庭内部儿童词汇卡片；如对外分发，请保留本文件与各自作者署名。",
        "",
    ]
    for slug, idx_list in picks.items():
        if slug not in by_slug:
            continue
        meta_path = CAND / slug / "meta.json"
        if not meta_path.exists():
            print(f"  ! {slug}: 没有候选记录")
            continue
        meta = json.load(open(meta_path))
        for idx in idx_list if isinstance(idx_list, list) else [idx_list]:
            if not isinstance(idx, int) or idx < 0 or idx >= len(meta):
                continue
            cand = meta[idx]
            dest = PHOTOS / f"{slug}.jpg"
            # 优先用 900px 版本重新下载（url 是原图地址，用 Special:FilePath 控制宽度更稳）
            file_title = cand["title"].replace(" ", "_")
            url900 = f"https://commons.wikimedia.org/wiki/Special:FilePath/{file_title}?width=900"
            if not curl_file(url900, dest):
                if not curl_file(cand["thumb"], dest):
                    print(f"  ! {slug}: 下载失败")
                    continue
            w = by_slug[slug]
            w["photo"] = f"photos/{slug}.jpg"
            w["photoTitle"] = clean(cand["title"])
            w["photoLicense"] = cand.get("license", "")
            made += 1
            lines.append(
                f"- **{w['word']}** — {clean(cand['title'])} — 作者：{cand.get('artist','')} — "
                f"许可：{cand.get('license','')} — {cand.get('page','')}"
            )
            break
    json.dump(words_data, open(args.words_out, "w"), ensure_ascii=False, indent=1)
    ATTRIB.write_text("\n".join(lines) + "\n")
    print(f"落地完成：{made} 张照片 → {PHOTOS}，已写入 {args.words_out}")


# ---------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description="为词表抓取真实照片（Wikimedia Commons）")
    ap.add_argument("--fetch", action="store_true", help="抓取候选图")
    ap.add_argument("--finalize", action="store_true", help="按 picks.json 下载最终图并写回 words.json")
    ap.add_argument("--words-out", default=str(DEFAULT_WORDS), help="words.json 路径")
    ap.add_argument("--only", default="", help="只处理这些词（逗号分隔）")
    ap.add_argument("--level", type=int, default=0, help="只处理某个等级（1-24）")
    ap.add_argument("--candidates", type=int, default=8, help="每个词最多保留几张候选")
    ap.add_argument("--jobs", type=int, default=3, help="并发图片下载数")
    ap.add_argument("--force", action="store_true", help="忽略缓存重新抓")
    ap.add_argument("--sheet-cols", type=int, default=4, help="拼图每行候选数")
    args = ap.parse_args()

    data = json.load(open(args.words_out))
    words = data["words"]
    if args.level:
        words = [w for w in words if w.get("level") == args.level]
    if args.only:
        wanted = {slugify(x) for x in args.only.split(",") if x.strip()}
        words = [w for w in words if slugify(w["word"]) in wanted]
    # 去重（同一个词跨等级只抓一次）
    seen_slugs: set[str] = set()
    uniq = []
    for w in words:
        s = slugify(w["word"])
        if s in seen_slugs:
            continue
        seen_slugs.add(s)
        uniq.append(w["word"])
    if not uniq:
        print("没有要处理的词")
        return

    if args.fetch:
        print(f"抓取候选：{len(uniq)} 个词，每词最多 {args.candidates} 张 …")
        ok = 0
        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
            for i, (word, n) in enumerate(ex.map(lambda w: fetch_word(w, args), uniq), 1):
                ok += 1 if n else 0
                print(f"  [{i}/{len(uniq)}] {word}: {n} 张候选")
        print(f"完成：{ok}/{len(uniq)} 个词有候选")
        sheets = build_sheets(uniq, cols=args.sheet_cols)
        print("拼图：")
        for s in sheets:
            print(" ", s)
        print("\n下一步：看拼图 → 写 photos/picks.json → 运行 --finalize")

    if args.finalize:
        finalize(args)


if __name__ == "__main__":
    main()
