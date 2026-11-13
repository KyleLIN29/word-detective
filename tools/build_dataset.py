#!/usr/bin/env python3
"""把用户提供的 Excel 词表转成测试用的 words.json，并批量下载配图。

输入：一份按等级分 sheet 的 Excel（默认 小小优趣完整单词表.xlsx，形如 P1-124 … P24-826）。
输出：
  - words.json            只包含「有可靠配图」的词，按原始等级分组
  - images/<word>.svg     每个词的配图（OpenMoji，CC BY-SA 4.0）
  - data/coverage.csv     全部词的处理结果（可测 / 短语 / 无配图 / 图标重复），方便补图
  - data/coverage-summary.md  各等级覆盖率汇总

匹配规则（宁可少配，不要配错）：
  1. 人工表 tools/icon_overrides.json 优先；
  2. OpenMoji 图标名称与单词完全一致（含复数还原）；
  3. 图标名称以该单词结尾（如 birthday cake → cake、police car → car）；
  4. 其余一律不自动配图，写进 coverage.csv 等人工补。

用法：
  python3 tools/build_dataset.py --dry-run     # 只看统计，不写文件、不下载
  python3 tools/build_dataset.py               # 生成 words.json 并下载配图
  python3 tools/build_dataset.py --force       # 重新下载已有配图
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_icons as fi  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_XLSX = Path.home() / "Desktop" / "小小优趣完整单词表.xlsx"

# 适合出「看图选词」题目的图标分组：具体名词、动物、食物、物品、交通、人物、活动
CONCRETE_GROUPS = {
    "food-drink",
    "animals-nature",
    "objects",
    "travel-places",
    "activities",
    "people-body",
}

# 24 个等级用一圈色相区分，卡片上的强调色
LEVEL_COLORS = [
    "#f4a261", "#2a9d8f", "#e76f51", "#4a8fe7", "#b5838d", "#6a994e",
    "#d68c45", "#3d8b8b", "#c0504d", "#7b6cf6", "#e08e45", "#2f9e6f",
    "#e4572e", "#4f7cac", "#b56576", "#6c8b3c", "#c97b3a", "#3fa796",
    "#a44a4a", "#5e6fb3", "#d98f3a", "#3f7d5e", "#c46a2f", "#6d7f9c",
]


def norm_text(s: str) -> str:
    return s.replace(" ", "").replace("-", "").replace("’", "'").strip().lower()


def is_single_word(raw: str) -> bool:
    w = raw.strip().strip(".").strip()
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z'’\-]*", w))


def singular_forms(word: str) -> list[str]:
    base = word.strip().strip(".").lower()
    return [f for f in dict.fromkeys(fi.singular_forms(base)) if f]


def safe_slug(word: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", word.strip().lower()).strip("-")
    return slug or "word"


# ---------------- 读取 Excel ----------------

class Sheet:
    def __init__(self, level_id: int, name: str, sheet_name: str, words: list[str]):
        self.id = level_id
        self.name = name
        self.sheet_name = sheet_name
        self.words = words


def read_sheets(xlsx: Path) -> list[Sheet]:
    wb = load_workbook(xlsx, data_only=True, read_only=True)
    sheets: list[Sheet] = []
    for ws in wb.worksheets:
        words: list[str] = []
        for row in ws.iter_rows(min_row=1, values_only=True):
            cells = [str(v).strip() for v in row if v is not None and str(v).strip() != ""]
            # 数据行 = 第一列是序号，第二非空列是单词；表头/说明行自动跳过
            if len(cells) >= 2 and re.fullmatch(r"\d+", cells[0]):
                words.append(cells[1])
        # 同一 sheet 内去重（大小写不敏感，保留首次出现的写法）
        seen = set()
        uniq = []
        for w in words:
            key = w.lower()
            if key not in seen:
                seen.add(key)
                uniq.append(w)
        m = re.search(r"P(\d+)", ws.title, re.I)
        level_id = int(m.group(1)) if m else len(sheets) + 1
        name = f"P{level_id}"
        sheets.append(Sheet(level_id, name, ws.title, uniq))
    # 如果 id 有重复/断档，按工作表顺序重新编号
    ids = [s.id for s in sheets]
    if len(set(ids)) != len(ids):
        for i, s in enumerate(sheets, 1):
            s.id = i
            s.name = f"P{i}"
    return sheets


# ---------------- 图标匹配 ----------------

FUNC_WORDS = {
    "on", "in", "up", "with", "of", "to", "for", "at", "under", "over", "and", "or",
    "by", "from", "into", "out", "off", "near", "next", "front", "behind", "between",
    "inside", "outside", "a", "an", "the", "your", "my", "his", "her", "their", "its",
}


def tail_match_ok(annotation: str) -> bool:
    """图标名必须是「修饰词 + 名词」这种简单两词短语，才允许按词尾匹配。"""
    toks = re.findall(r"[a-z0-9']+", annotation.lower())
    if len(toks) != 2:
        return False
    if any(t in FUNC_WORDS for t in toks):
        return False
    if any(t.endswith("ing") and len(t) > 4 for t in toks):
        return False
    return not any(ch.isupper() for ch in annotation)


class IconMatcher:
    def __init__(self, entries: list[dict], overrides: dict[str, str]):
        self.by_hex = {e.get("hexcode", "").upper(): e for e in entries}
        self.by_char = {fi.norm_emoji(e.get("emoji", "")): e for e in entries if e.get("emoji")}
        self.overrides = overrides
        self.exact: dict[str, list[dict]] = {}
        self.tail: dict[str, list[dict]] = {}
        for e in entries:
            ann = e.get("annotation", "").strip().lower()
            if not ann:
                continue
            self.exact.setdefault(norm_text(ann), []).append(e)
            toks = re.findall(r"[a-z0-9']+", ann)
            if len(toks) >= 2:
                self.tail.setdefault(toks[-1], []).append(e)
        self.cache: dict[str, tuple[dict | None, int, float]] = {}

    def _rank(self, word: str, entry: dict, quality: int) -> float:
        ann = entry.get("annotation", "")
        score = quality * 100 - len(ann) * 0.3
        score += fi.GROUP_BONUS.get(entry.get("group", ""), 0)
        if entry.get("skintone"):
            score -= 15
        if "\u200d" in entry.get("emoji", ""):
            score -= 6
        return score

    def find(self, word: str) -> tuple[dict | None, int, float]:
        """返回 (图标条目, 质量 2/3, 排序分)。质量 3 = 名称完全一致，2 = 名称尾部一致。"""
        key = word.strip().strip(".").lower()
        if key in self.cache:
            return self.cache[key]

        # 1) 人工指定
        for form in singular_forms(word):
            if form in self.overrides:
                entry = fi.resolve_override(self.overrides[form], self.by_hex, self.by_char)
                if entry:
                    res = (entry, 3, 999.0)
                    self.cache[key] = res
                    return res

        # 2) 自动匹配：图标名称完全相同（质量 3），或「修饰词+名词」两词短语的末尾
        #    与单词一致（质量 2，如 police car → car、birthday cake → cake）。
        best: tuple[dict | None, int, float] = (None, 0, -1.0)
        for form in singular_forms(word):
            candidates: dict[str, int] = {}
            for e in self.exact.get(norm_text(form), []):
                candidates[e["hexcode"]] = 3
            for e in self.tail.get(form, []):
                candidates.setdefault(e["hexcode"], 2)
            for e in candidates.values() if False else []:
                pass
            for hexcode, quality in candidates.items():
                e = self.by_hex.get(hexcode)
                if not e or e.get("group", "") not in CONCRETE_GROUPS:
                    continue
                if quality == 2 and not tail_match_ok(e.get("annotation", "")):
                    continue
                score = self._rank(word, e, quality)
                if score > best[2]:
                    best = (e, quality, score)
        self.cache[key] = best
        return best


def download_icons(jobs: list[tuple[str, str, Path]], force: bool, workers: int = 8) -> int:
    """jobs = [(hexcode, url, dest)]，返回成功数量。"""

    def one(job: tuple[str, str, Path]) -> bool:
        hexcode, _url, dest = job
        return fi.download_svg(hexcode, dest, force)

    ok = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(one, job): job for job in jobs}
        for fut in as_completed(futures):
            try:
                if fut.result():
                    ok += 1
            except Exception as exc:  # noqa: BLE001
                print(f"   ⚠️ 下载失败：{exc}", file=sys.stderr)
    return ok


# ---------------- 主流程 ----------------

def main() -> int:
    parser = argparse.ArgumentParser(description="把 Excel 词表转成 words.json + 配图")
    parser.add_argument("--xlsx", default=str(DEFAULT_XLSX), help="词表 Excel 路径")
    parser.add_argument("--words-out", default=str(ROOT / "words.json"))
    parser.add_argument("--images", default=str(ROOT / "images"))
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    parser.add_argument("--cache", default=str(ROOT / "tools" / ".cache" / "openmoji.json"))
    parser.add_argument("--overrides", default=str(ROOT / "tools" / "icon_overrides.json"))
    parser.add_argument("--blocklist", default=str(ROOT / "tools" / "blocklist.json"),
                        help="自动配图会误导、需要人工补图的单词")
    parser.add_argument("--min-per-level", type=int, default=3, help="少于这些可测词的等级跳过")
    parser.add_argument("--jobs", type=int, default=8, help="下载配图的并发数")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写文件、不下载")
    parser.add_argument("--force", action="store_true", help="重新下载已有配图")
    parser.add_argument("--refresh-data", action="store_true", help="强制重新下载 OpenMoji 索引")
    args = parser.parse_args()

    xlsx = Path(args.xlsx)
    if not xlsx.exists():
        print(f"❌ 找不到词表：{xlsx}")
        print("   用 --xlsx 指定 Excel 路径，或把词表放到桌面并保持原名。")
        return 1

    print(f"📄 读取词表：{xlsx.name}")
    sheets = read_sheets(xlsx)
    total_words = sum(len(s.words) for s in sheets)
    print(f"   {len(sheets)} 个等级，共 {total_words} 行（去重后仍按等级计）")

    # 每个等级累计的唯一「单词」数（不含短语），用于自适应测级估算词汇量
    cum_single: dict[int, int] = {}
    seen_single: set[str] = set()
    for sheet in sheets:
        for raw in sheet.words:
            text = raw.strip().strip(".")
            if " " in text or not is_single_word(raw):
                continue
            seen_single.add(text.lower())
        cum_single[sheet.id] = len(seen_single)
    print(f"   累计唯一单词：{cum_single.get(sheets[-1].id, 0)} 个")

    blocklist_path = Path(args.blocklist)
    blocked: set[str] = set()
    if blocklist_path.exists():
        raw = json.loads(blocklist_path.read_text(encoding="utf-8"))
        blocked = {str(x).strip().lower() for x in raw.get("blocked", [])}

    entries = fi.ensure_index(Path(args.cache), args.refresh_data)
    matcher = IconMatcher(entries, fi.load_overrides(Path(args.overrides)))
    print(f"📚 OpenMoji 索引：{len(entries)} 个图标；人工指定 {len(matcher.overrides)} 个词")
    bad = [k for k, v in matcher.overrides.items()
           if fi.resolve_override(v, matcher.by_hex, matcher.by_char) is None]
    if bad:
        print(f"⚠️  有 {len(bad)} 条人工配图找不到对应图标（会被忽略）：{', '.join(bad[:12])}"
              + (" …" if len(bad) > 12 else ""))

    image_dir = Path(args.images)
    data_dir = Path(args.data_dir)
    image_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    # slug 全局唯一（不同拼写映射到同一 slug 时加序号）
    slug_of: dict[str, str] = {}
    used_slugs: set[str] = set()

    def slug_for(word: str) -> str:
        key = word.strip().lower()
        if key in slug_of:
            return slug_of[key]
        base = safe_slug(word)
        slug = base
        n = 2
        while slug in used_slugs:
            slug = f"{base}-{n}"
            n += 1
        used_slugs.add(slug)
        slug_of[key] = slug
        return slug

    levels_out: list[dict] = []
    words_out: list[dict] = []
    coverage_rows: list[dict] = []
    downloads: dict[Path, tuple[dict, str]] = {}  # dest -> (entry, hexcode)
    total_testable = 0
    last_included_cum = 0

    for idx, sheet in enumerate(sheets):
        level_words: list[dict] = []
        used_icons: set[str] = set()
        stats = {"total": len(sheet.words), "testable": 0, "phrase": 0,
                 "no_icon": 0, "low_confidence": 0, "icon_duplicate": 0}
        for raw in sheet.words:
            word = raw.strip().strip(".")
            row = {
                "level": sheet.name, "sheet": sheet.sheet_name, "word": raw,
                "status": "", "reason": "", "icon": "", "annotation": "",
                "group": "", "quality": "", "score": "",
            }
            if " " in word.strip() or not is_single_word(raw):
                stats["phrase"] += 1
                row.update(status="excluded", reason="短语/句子，无法用单图测试")
                coverage_rows.append(row)
                continue

            if word.lower() in blocked:
                stats["no_icon"] += 1
                row.update(status="excluded", reason="人工屏蔽（自动配图会误导，需补图）")
                coverage_rows.append(row)
                continue

            entry, quality, score = matcher.find(word)
            if not entry:
                stats["no_icon"] += 1
                row.update(status="excluded", reason="没有匹配到图标")
                coverage_rows.append(row)
                continue
            hexcode = entry["hexcode"].upper()
            row.update(icon=entry.get("emoji", ""), annotation=entry.get("annotation", ""),
                       group=entry.get("group", ""), quality=str(quality), score=f"{score:.0f}")
            if hexcode in used_icons:
                stats["icon_duplicate"] += 1
                row.update(status="excluded", reason="同等级内图标重复")
                coverage_rows.append(row)
                continue
            used_icons.add(hexcode)
            stats["testable"] += 1
            row.update(status="testable", reason="")
            coverage_rows.append(row)

            slug = slug_for(word)
            word_id = f"p{sheet.id}-{slug}"
            level_words.append({
                "id": word_id,
                "word": word,
                "level": sheet.id,
                "image": f"images/{slug}.svg",
                "emoji": entry.get("emoji", ""),
                "audio": f"audio/{slug}.m4a",
                "group": entry.get("group", ""),
                "iconSource": f"OpenMoji {hexcode}",
            })
            dest = image_dir / f"{slug}.svg"
            downloads.setdefault(dest, (entry, hexcode))

        percent = round(stats["testable"] / stats["total"] * 100) if stats["total"] else 0
        if stats["testable"] >= args.min_per_level:
            level_cum = cum_single.get(sheet.id, 0)
            levels_out.append({
                "id": sheet.id,
                "name": sheet.name,
                "color": LEVEL_COLORS[idx % len(LEVEL_COLORS)],
                "sheet": sheet.sheet_name,
                "cumulativeWords": level_cum,
                "newWords": max(0, level_cum - last_included_cum),
                "coverage": {
                    "total": stats["total"],
                    "testable": stats["testable"],
                    "percent": percent,
                    "phrase": stats["phrase"],
                    "noIcon": stats["no_icon"],
                    "iconDuplicate": stats["icon_duplicate"],
                },
            })
            words_out.extend(level_words)
            total_testable += stats["testable"]
            last_included_cum = level_cum
        print(
            f"  {sheet.name:>4} 共 {stats['total']:>4} 词 → 可测 {stats['testable']:>4}"
            f"（{percent:>3}%） 短语 {stats['phrase']:>3} · 缺图 {stats['no_icon']:>3}"
            + (f" · 重复图标 {stats['icon_duplicate']}" if stats["icon_duplicate"] else "")
            + ("" if stats["testable"] >= args.min_per_level else "  ← 可测词不足，未加入测试")
        )

    covered_levels = len(levels_out)
    print(f"\n✅ 测试数据：{covered_levels} 个等级，{total_testable} 个词，"
          f"共需 {len(downloads)} 张配图")

    if args.dry_run:
        print("（dry-run：未写文件、未下载图片）")
        return 0

    # 下载配图
    jobs = [(hexcode, "", dest) for dest, (_entry, hexcode) in downloads.items()]
    missing = [job for job in jobs if not job[2].exists()]
    print(f"⬇️  下载配图：{len(jobs) - len(missing)} 张已存在，{len(missing)} 张待下载 …")
    started = time.time()
    ok = download_icons(jobs, args.force, workers=args.jobs)
    print(f"   完成 {ok}/{len(jobs)} 张，用时 {time.time() - started:.1f}s")

    data = {
        "meta": {
            "title": "单词小侦探",
            "subtitle": "听一听，选出正确的图片",
            "note": "词表来自小小优趣完整单词表；只测试有配图的词，覆盖率见等级卡。",
            "defaultQuestions": 8,
            "source": xlsx.name,
            "generatedAt": time.strftime("%Y-%m-%d %H:%M"),
            "levels": covered_levels,
            "testableWords": total_testable,
            "totalUniqueWords": cum_single.get(sheets[-1].id, 0),
        },
        "levels": levels_out,
        "words": words_out,
    }
    words_path = Path(args.words_out)
    words_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # 覆盖率明细（方便补图）
    csv_path = data_dir / "coverage.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(coverage_rows[0].keys()))
        writer.writeheader()
        writer.writerows(coverage_rows)

    # 覆盖率汇总
    md_lines = [
        "# 词表配图覆盖率",
        "",
        f"- 词表：`{xlsx.name}`",
        f"- 生成时间：{data['meta']['generatedAt']}",
        f"- 等级：{covered_levels} 个；可测词：{total_testable} 个",
        "",
        "| 等级 | 原词数 | 可测 | 覆盖率 | 短语 | 缺图 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for lv in levels_out:
        c = lv["coverage"]
        md_lines.append(
            f"| {lv['name']} | {c['total']} | {c['testable']} | {c['percent']}% | "
            f"{c['phrase']} | {c['noIcon']} |"
        )
    md_lines += [
        "",
        "> 「短语」是句子/词组（如 `Good morning!`），需要情景图才能测试；",
        "> 「缺图」是 OpenMoji 没有合适图标的单词，可以把需要补图的词填进 `tools/icon_overrides.json`，",
        "> 或按 `data/coverage.csv` 里 `status=excluded` 的清单人工补图。",
        "",
        "图标版权：OpenMoji（CC BY-SA 4.0），详见 `images/ATTRIBUTION.md`。",
    ]
    (data_dir / "coverage-summary.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    print(f"\n📝 {words_path}")
    print(f"📝 {csv_path}")
    print(f"📝 {data_dir / 'coverage-summary.md'}")
    print("\n下一步：python3 tools/make_audio.py   （为新增的词生成音频）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
