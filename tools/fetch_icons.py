#!/usr/bin/env python3
"""批量给单词配图：从 OpenMoji 开源图标库匹配并下载 SVG。

OpenMoji 用 emoji 做索引，所以「词 → 图标」是自动匹配的：
  - 先按单次匹配 annotation / tags（带词形还原，shoe/shoes、apple/apples 都能匹配）
  - 再按匹配质量和分组排序，优先动物 / 食物 / 物品这类具体图标
  - tools/icon_overrides.json 可以人工指定某个词用哪个图标（覆盖自动结果）

第一次运行会下载 OpenMoji 的索引（约 2 MB），之后离线运行即可。
下载好的图标存在 images/ 里，最终网页运行完全不联网。

用法：
  python3 tools/fetch_icons.py --dry-run        # 只看匹配结果，不下载
  python3 tools/fetch_icons.py                  # 下载并写回 words.json
  python3 tools/fetch_icons.py --only apple,banana
  python3 tools/fetch_icons.py --force          # 重新下载已有图片、覆盖人工配图

图标版权：OpenMoji (https://openmoji.org)，CC BY-SA 4.0。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPENMOJI_INDEX_URL = "https://raw.githubusercontent.com/hfg-gmuend/openmoji/master/data/openmoji.json"
OPENMOJI_SVG_URL = "https://raw.githubusercontent.com/hfg-gmuend/openmoji/master/color/svg/{hexcode}.svg"
LICENSE_URL = "https://openmoji.org/license"

# 分组偏好：具体、可命名的图标优先；表情脸 / 符号 / 旗帜靠后
GROUP_BONUS = {
    "food-drink": 8,
    "animals-nature": 8,
    "objects": 6,
    "activities": 4,
    "travel-places": 4,
    "people-body": 3,
    "smileys-emotion": -6,
    "symbols": -8,
    "extras-unicode": -10,
    "extras-openmoji": -12,
    "flags": -20,
    "component": -30,
}
VARIATION_SELECTOR = "\ufe0f"


def norm_emoji(s: str) -> str:
    return s.replace(VARIATION_SELECTOR, "")


def singular_forms(word: str) -> list[str]:
    """简单的复数/词形还原，够用于名词词表。"""
    forms = [word]
    if word.endswith("ies") and len(word) > 4:
        forms.append(word[:-3] + "y")
    if word.endswith("es") and len(word) > 3:
        forms.append(word[:-2])
    if word.endswith("s") and not word.endswith("ss") and len(word) > 2:
        forms.append(word[:-1])
    if word.endswith("ing") and len(word) > 5:
        forms.append(word[:-3])
        forms.append(word[:-3] + "e")
    return list(dict.fromkeys(forms))


def load_overrides(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(k).strip().lower(): str(v).strip() for k, v in data.items() if str(v).strip()}
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️  {path.name} 解析失败，已忽略：{exc}", file=sys.stderr)
        return {}


def ensure_index(cache: Path, refresh: bool) -> list[dict]:
    cache.parent.mkdir(parents=True, exist_ok=True)
    if cache.exists() and not refresh:
        print(f"📖 使用本地 OpenMoji 索引：{cache}")
    else:
        print("⬇️  下载 OpenMoji 索引（仅第一次需要联网，约 2 MB）…")
        with urllib.request.urlopen(OPENMOJI_INDEX_URL, timeout=120) as resp:
            raw = resp.read()
        cache.write_bytes(raw)
        print(f"   已缓存到 {cache}")
    return json.loads(cache.read_text(encoding="utf-8"))


def build_index(entries: list[dict]) -> dict[str, list[dict]]:
    index: dict[str, list[dict]] = {}

    def add(token: str, entry: dict) -> None:
        token = token.strip().lower()
        if len(token) < 2:
            return
        index.setdefault(token, []).append(entry)

    for entry in entries:
        ann = entry.get("annotation", "").strip().lower()
        add(ann, entry)
        for tok in re.split(r"[^a-z]+", ann):
            add(tok, entry)
        for tag in str(entry.get("tags", "")).split(","):
            tag = tag.strip().lower()
            add(tag, entry)
            for tok in re.split(r"[^a-z]+", tag):
                add(tok, entry)
    return index


def resolve_override(value: str, by_hex: dict[str, dict], by_char: dict[str, dict]) -> dict | None:
    key = norm_emoji(value)
    if key in by_char:
        return by_char[key]
    return by_hex.get(value.upper().strip())


def score_entry(entry: dict, word: str) -> float:
    ann = entry.get("annotation", "").lower().strip()
    tags = [t.strip().lower() for t in str(entry.get("tags", "")).split(",") if t.strip()]
    score = 0.0
    if ann == word:
        score = 100.0
    elif ann.replace(" ", "") == word:
        score = 95.0
    elif word in tags:
        score = 55.0
    elif re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", ann):
        score = 45.0
    elif any(tag.startswith(word) or word in re.findall(r"[a-z]+", tag) for tag in tags):
        score = 25.0
    else:
        return -1.0
    score += GROUP_BONUS.get(entry.get("group", ""), 0)
    score -= len(ann) * 0.15
    # 带人种肤色的变体、带性别的组合图标，不适合做儿童词卡
    if entry.get("skintone") not in ("", None):
        score -= 15
    if "\u200d" in entry.get("emoji", ""):
        score -= 6
    return score


def pick_icon(word: str, index: dict[str, list[dict]], overrides: dict[str, str],
              by_hex: dict[str, dict], by_char: dict[str, dict]) -> tuple[dict | None, float]:
    for form in singular_forms(word.lower()):
        if form in overrides:
            entry = resolve_override(overrides[form], by_hex, by_char)
            if entry:
                return entry, 999.0
    candidates: dict[str, dict] = {}
    for form in singular_forms(word.lower()):
        for entry in index.get(form, []):
            candidates[entry.get("hexcode", "")] = entry
    scored = sorted(
        ((score_entry(e, f), e) for f in singular_forms(word.lower()) for e in candidates.values()
         if e.get("hexcode")),
        key=lambda pair: pair[0],
        reverse=True,
    )
    if not scored:
        return None, -1.0
    best_score, best = scored[0]
    # 开源图标库的「extras」里有不少是补字用的图标（例如 Apple logo 的 annotation 就是 apple）。
    # 只要普通图标里有像样的匹配，就优先用普通图标。
    if best.get("group", "").startswith("extras"):
        for s, e in scored:
            if not e.get("group", "").startswith("extras") and s >= 40:
                return e, s
    return best, best_score


def download_svg(hexcode: str, dest: Path, force: bool) -> bool:
    if dest.exists() and not force:
        return True
    url = OPENMOJI_SVG_URL.format(hexcode=hexcode.upper())
    req = urllib.request.Request(url, headers={"User-Agent": "vocab-test-icon-fetcher/1.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
            dest.write_bytes(data)
            return True
        except (urllib.error.URLError, TimeoutError) as exc:  # noqa: PERF203
            if attempt == 2:
                print(f"   ⚠️  下载失败 {url}：{exc}", file=sys.stderr)
                return False
            time.sleep(1.5)
    return False


def safe_name(word_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", word_id).strip("-") or "word"


def main() -> int:
    parser = argparse.ArgumentParser(description="从 OpenMoji 给单词自动配图")
    parser.add_argument("--words", default=str(ROOT / "words.json"))
    parser.add_argument("--images", default=str(ROOT / "images"))
    parser.add_argument("--cache", default=str(ROOT / "tools" / ".cache" / "openmoji.json"))
    parser.add_argument("--overrides", default=str(ROOT / "tools" / "icon_overrides.json"))
    parser.add_argument("--only", default="", help="只处理这些 id/单词，逗号分隔")
    parser.add_argument("--dry-run", action="store_true", help="只显示匹配结果，不下载不写文件")
    parser.add_argument("--force", action="store_true", help="重新下载并覆盖已有的图片配置")
    parser.add_argument("--refresh-data", action="store_true", help="强制重新下载 OpenMoji 索引")
    args = parser.parse_args()

    words_path = Path(args.words)
    images_dir = Path(args.images)
    data = json.loads(words_path.read_text(encoding="utf-8"))
    words = data.get("words", [])
    if not words:
        print("❌ words.json 里没有 words 数组", file=sys.stderr)
        return 1

    entries = ensure_index(Path(args.cache), args.refresh_data)
    index = build_index(entries)
    by_hex = {e.get("hexcode", "").upper(): e for e in entries}
    by_char = {norm_emoji(e.get("emoji", "")): e for e in entries if e.get("emoji")}
    overrides = load_overrides(Path(args.overrides))
    only = {s.strip().lower() for s in args.only.split(",") if s.strip()}

    report = {"matched": [], "unmatched": [], "unclear": []}
    for w in words:
        word = str(w.get("word") or w.get("id") or "").strip()
        wid = str(w.get("id") or word).strip()
        if not word:
            continue
        if only and word.lower() not in only and wid.lower() not in only:
            continue
        entry, score = pick_icon(word, index, overrides, by_hex, by_char)
        if not entry:
            report["unmatched"].append(word)
            print(f"   ✗ {word:<22} 没有找到合适的图标，需要人工配图")
            continue
        icon, ann, group = entry["emoji"], entry["annotation"], entry.get("group", "")
        if score < 40:
            report["unclear"].append({"word": word, "icon": icon, "annotation": ann})
        if args.dry_run:
            report["matched"].append({"word": word, "icon": icon, "annotation": ann})
            print(f"   {icon}  {word:<20} ← {ann:<28} [{group}] score={score:.0f}")
            continue

        dest = images_dir / f"{safe_name(wid)}.svg"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not download_svg(entry["hexcode"], dest, args.force):
            report["unmatched"].append(word)
            continue
        report["matched"].append({"word": word, "icon": icon, "annotation": ann, "file": dest.name})

        current_image = str(w.get("image", ""))
        looks_like_path = bool(re.match(r"^(https?:|data:|blob:|\.{0,2}/|/)", current_image)) or bool(
            re.search(r"\.(png|jpe?g|svg|webp|gif|avif)(\?|#|$)", current_image, re.I)
        )
        if not looks_like_path and current_image and not w.get("emoji"):
            w["emoji"] = current_image  # 保留原来的 emoji，图片加载失败时兜底
        if looks_like_path and not args.force:
            continue  # 已经有人工配图，不动
        w["image"] = f"images/{dest.name}"
        if not w.get("emoji"):
            w["emoji"] = icon
        w["iconSource"] = f"OpenMoji {entry['hexcode']}"

    if args.dry_run:
        print(f"\n共 {len(report['matched'])} 个可自动配图（dry-run，未写入文件）")
        if report["unmatched"]:
            print(f"⚠️  {len(report['unmatched'])} 个没有合适图标：{', '.join(report['unmatched'])}")
        return 0

    words_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    images_dir.mkdir(parents=True, exist_ok=True)
    (images_dir / "_icon-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (images_dir / "ATTRIBUTION.md").write_text(
        "# 图片来源\n\n"
        "本目录图标来自 [OpenMoji](https://openmoji.org)，许可协议 CC BY-SA 4.0。\n"
        f"许可说明：{LICENSE_URL}\n\n"
        "如需商用或上架，请保留署名；也可以整体替换成自己的插画。\n",
        encoding="utf-8",
    )
    print(f"\n✅ 已匹配 {len(report['matched'])} 个，下载到 {images_dir}")
    if report["unmatched"]:
        print(f"⚠️  {len(report['unmatched'])} 个没有合适图标：{', '.join(report['unmatched'][:30])}"
              + (" …" if len(report["unmatched"]) > 30 else ""))
        print("   把需要人工配图的词补到 images/ 下，或在 tools/icon_overrides.json 里手动指定图标。")
    if report["unclear"]:
        print(f"🤔 {len(report['unclear'])} 个匹配把握不大，建议人工过一眼："
              + ", ".join(f"{x['word']}→{x['icon']}" for x in report["unclear"][:15])
              + (" …" if len(report["unclear"]) > 15 else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
