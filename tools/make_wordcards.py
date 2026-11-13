#!/usr/bin/env python3
"""生成前 4 周词卡 HTML（图片来自 words.json 的 OpenMoji 配图）。

用法：
    python3 tools/make_wordcards.py [--words-out words.json] [--out plan/word-cards.html]
"""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 每周：(主题, 儿歌建议, [(单词, 中文, 周几), ...])
WEEKS = [
    (
        "动物朋友",
        "Old MacDonald Had a Farm",
        [
            ("dog", "狗", "周一"),
            ("cat", "猫", "周一"),
            ("duck", "鸭子", "周一"),
            ("frog", "青蛙", "周二"),
            ("pig", "猪", "周二"),
            ("cow", "奶牛", "周三"),
            ("horse", "马", "周三"),
            ("elephant", "大象", "周三"),
            ("rooster", "公鸡", "周四"),
            ("shark", "鲨鱼", "周四"),
        ],
    ),
    (
        "水果和颜色",
        "Apple Round Apple Red / Color Song",
        [
            ("apple", "苹果", "周一"),
            ("banana", "香蕉", "周一"),
            ("orange", "橙子", "周一"),
            ("red", "红色", "周二"),
            ("green", "绿色", "周二"),
            ("blue", "蓝色", "周三"),
            ("yellow", "黄色", "周三"),
            ("purple", "紫色", "周四"),
            ("brown", "棕色", "周四"),
        ],
    ),
    (
        "家人和身体",
        "Finger Family / Head Shoulders Knees and Toes",
        [
            ("mommy", "妈妈", "周一"),
            ("daddy", "爸爸", "周一"),
            ("baby", "宝宝", "周一"),
            ("grandma", "奶奶", "周二"),
            ("grandpa", "爷爷", "周二"),
            ("eyes", "眼睛", "周二"),
            ("ears", "耳朵", "周三"),
            ("nose", "鼻子", "周三"),
            ("mouth", "嘴", "周三"),
            ("hands", "手", "周四"),
            ("feet", "脚", "周四"),
            ("leg", "腿", "周四"),
        ],
    ),
    (
        "数字 1–10",
        "Ten Little Indians / Numbers Song 1–10",
        [
            ("one", "一", "周一"),
            ("two", "二", "周一"),
            ("three", "三", "周一"),
            ("four", "四", "周二"),
            ("five", "五", "周二"),
            ("six", "六", "周二"),
            ("seven", "七", "周三"),
            ("eight", "八", "周三"),
            ("nine", "九", "周三"),
            ("ten", "十", "周四"),
            ("yes", "对 / 是", "周四"),
            ("no", "不", "周四"),
        ],
    ),
]

CARD_CSS = """
  @page { size: A4; margin: 9mm 9mm 8mm; }
  * { box-sizing: border-box; }
  body {
    margin: 0; color: #2f2a26; background: #fff;
    font-family: "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
  }
  .page { page-break-after: always; }
  .page:last-child { page-break-after: auto; }
  .page-head {
    display: flex; align-items: baseline; justify-content: space-between;
    border-bottom: 2px solid #f4a261; padding-bottom: 4px; margin-bottom: 6px;
  }
  .page-head h1 { font-size: 16px; margin: 0; }
  .page-head .hint { font-size: 9.5px; color: #8a7f74; }
  .grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 5mm; }
  .card {
    position: relative; border: 1.5px dashed #d8c9b8; border-radius: 4mm;
    height: 58mm; padding: 3mm 3mm 2mm; display: flex; flex-direction: column;
    align-items: center; justify-content: space-between; background: #fffdf9;
  }
  .card .tag {
    align-self: flex-start; font-size: 8.5px; color: #2a9d8f; background: #eaf4f2;
    padding: 0.3mm 2mm; border-radius: 2mm;
  }
  .card .pic { width: 30mm; height: 30mm; object-fit: contain; }
  .card .emoji { font-size: 26mm; line-height: 30mm; }
  .card .word {
    font-family: "Avenir Next", "PingFang SC", sans-serif;
    font-size: 19px; font-weight: 700; letter-spacing: .3px; margin-top: 1mm;
  }
  .card .zh { font-size: 9.5px; color: #8a7f74; margin-top: -1mm; }
  .card.blank { border-style: solid; border-color: #f2e9df; background: #fff; }
  .note { margin-top: 3mm; font-size: 9px; color: #a2968a; }
"""


def pick_word(by_word: dict[str, dict], word: str) -> dict:
    obj = by_word.get(word.lower())
    if not obj:
        raise SystemExit(f"❌ words.json 里找不到单词：{word}")
    image = obj.get("image") or ""
    if not image:
        raise SystemExit(f"❌ 单词没有配图：{word}")
    return obj


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--words-out", default=str(ROOT / "words.json"))
    parser.add_argument("--out", default=str(ROOT / "plan" / "word-cards.html"))
    args = parser.parse_args()

    data = json.loads(Path(args.words_out).read_text(encoding="utf-8"))
    by_word: dict[str, dict] = {}
    for obj in data.get("words", []):
        key = str(obj.get("word", "")).strip().lower()
        if key and key not in by_word:
            by_word[key] = obj

    parts: list[str] = []
    total = 0
    for wi, (theme, song, items) in enumerate(WEEKS, 1):
        cards: list[str] = []
        for word, zh, day in items:
            obj = pick_word(by_word, word)
            image = html.escape(str(obj["image"]))
            emoji = html.escape(str(obj.get("emoji", "")))
            cards.append(
                f"""<div class="card">
      <span class="tag">第 {wi} 周 · {day}</span>
      <img class="pic" src="../{image}" alt="{html.escape(word)}">
      <div class="word">{html.escape(word)}</div>
      <div class="zh">{html.escape(zh)}</div>
    </div>"""
            )
            total += 1
        parts.append(
            f"""<section class="page">
  <div class="page-head">
    <h1>第 {wi} 周 · {theme}（{len(items)} 词）</h1>
    <div class="hint">儿歌：{song}　·　贴纸：⭐</div>
  </div>
  <div class="grid">
    {''.join(cards)}
  </div>
  <p class="note">家长说单词（可点开测试网页听发音），孩子从 3 张卡里指出正确的那张；周五用整周卡片做小测。</p>
</section>"""
        )

    html_out = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>前 4 周词卡 · 3 岁英语启蒙</title>
<style>{CARD_CSS}</style>
</head>
<body>
{''.join(parts)}
</body>
</html>
"""

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html_out, encoding="utf-8")
    print(f"✅ 词卡：{total} 张，4 页 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
