#!/usr/bin/env python3
"""批量生成单词音频（macOS 自带 say + afconvert，无需额外安装）。

用法（在 vocab-test 目录下运行）：
  python3 tools/make_audio.py                 # 只为缺失音频的单词生成
  python3 tools/make_audio.py --force         # 全部重新生成
  python3 tools/make_audio.py --voice Samantha --rate 135
  python3 tools/make_audio.py --jobs 6        # 并发数，默认 4
  python3 tools/make_audio.py --only apple dog

说明：
- 读取 words.json；音频路径取自每条词的 "audio" 字段，留空则默认 audio/<id>.m4a。
- 同一个词在多个等级里重复时只生成一次。
- 朗读文本默认用 "word" 字段；可用可选的 "tts" 字段覆盖（例如 read 读作 reed）。
- 生成的是 macOS 系统 TTS 语音：清晰、离线、免费，适合原型和家庭自用。
  正式发行建议替换为真人录音，或 Azure / ElevenLabs 等更自然的 TTS（见 README）。
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORDS_JSON = ROOT / "words.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch-generate word pronunciation audio with macOS say.")
    parser.add_argument("--voice", default="Samantha", help="say 语音名，默认 Samantha（美式女声）")
    parser.add_argument("--rate", type=int, default=135, help="语速（词/分钟），默认 135，慢一点的儿童向语速")
    parser.add_argument("--jobs", type=int, default=4, help="并发数，默认 4")
    parser.add_argument("--force", action="store_true", help="已存在的音频也重新生成")
    parser.add_argument("--only", nargs="*", default=None, help="只处理这些 id（默认全部）")
    args = parser.parse_args()

    if sys.platform != "darwin":
        print("这个脚本依赖 macOS 的 say / afconvert。")
        return 1
    if shutil.which("say") is None or shutil.which("afconvert") is None:
        print("找不到 say 或 afconvert。")
        return 1

    data = json.loads(WORDS_JSON.read_text(encoding="utf-8"))
    words = data.get("words", [])
    if args.only:
        wanted = set(args.only)
        words = [w for w in words if w.get("id") in wanted]

    # 同一个词跨等级只生成一次
    tasks: dict[Path, dict] = {}
    for w in words:
        text = w.get("tts") or w.get("word")
        if not text:
            continue
        rel = w.get("audio") or f"audio/{w.get('id') or text}.m4a"
        tasks.setdefault(ROOT / rel, {**w, "text": text})
    if not tasks:
        print("words.json 里没有需要生成音频的词。")
        return 0

    tmpdir = Path(tempfile.mkdtemp(prefix="word-audio-"))
    lock = threading.Lock()
    counters = {"made": 0, "skipped": 0, "failed": 0}

    def one(out: Path, w: dict) -> None:
        if out.exists() and not args.force:
            with lock:
                counters["skipped"] += 1
            return
        out.parent.mkdir(parents=True, exist_ok=True)
        aiff = tmpdir / f"{out.stem}-{threading.get_ident()}.aiff"
        try:
            subprocess.run(
                ["say", "-v", args.voice, "-r", str(args.rate), "-o", str(aiff), w["text"]],
                check=True, capture_output=True,
            )
            subprocess.run(
                ["afconvert", "-f", "m4af", "-d", "aac", str(aiff), str(out)],
                check=True, capture_output=True,
            )
            with lock:
                counters["made"] += 1
        except subprocess.CalledProcessError as e:
            msg = (e.stderr or b"").decode("utf-8", "replace").strip()
            with lock:
                counters["failed"] += 1
            print(f"  ✗ {w['text']}: {msg or e}")
        finally:
            aiff.unlink(missing_ok=True)

    todo = [(out, w) for out, w in tasks.items()]
    print(f"🔊 共 {len(todo)} 个音频，并发 {args.jobs} …")
    try:
        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            futures = [pool.submit(one, out, w) for out, w in todo]
            for i, _fut in enumerate(as_completed(futures), 1):
                if i % 50 == 0 or i == len(futures):
                    print(f"   进度 {i}/{len(futures)}（新生成 {counters['made']}，跳过 {counters['skipped']}）")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print(f"\n完成：新生成 {counters['made']} 个，跳过 {counters['skipped']} 个，失败 {counters['failed']} 个。")
    return 0 if counters["failed"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
