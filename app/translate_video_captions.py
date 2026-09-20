import argparse
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


URL = "https://translate.googleapis.com/translate_a/single"
REPLACEMENTS = []
CHINESE_REPLACEMENTS = []


def stamp(value):
    ms = max(0, round(value * 1000))
    hour, ms = divmod(ms, 3_600_000)
    minute, ms = divmod(ms, 60_000)
    second, ms = divmod(ms, 1000)
    return f"{hour:02}:{minute:02}:{second:02},{ms:03}"


def write_srt(path, segments, field):
    with path.open("w", encoding="utf-8-sig", newline="\n") as stream:
        for number, segment in enumerate(segments, 1):
            text = re.sub(r"\s+", " ", segment[field]).strip()
            stream.write(f"{number}\n{stamp(segment['start'])} --> {stamp(max(segment['start'] + .1, segment['end']))}\n{text}\n\n")


def translate_batch(lines):
    query = urllib.parse.urlencode(
        {"client": "gtx", "sl": "en", "tl": "zh-CN", "dt": "t", "q": "\n".join(lines)}
    )
    request = urllib.request.Request(f"{URL}?{query}", headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=45) as response:
        parts = json.load(response)[0]
    result = "".join(part[0] or "" for part in parts).splitlines()
    if len(result) != len(lines):
        if len(lines) == 1:
            raise ValueError("Translation response did not preserve one subtitle line")
        midpoint = len(lines) // 2
        return translate_batch(lines[:midpoint]) + translate_batch(lines[midpoint:])
    return [part.strip() for part in result]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("video", type=Path)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    video_id = re.search(r"\[([A-Za-z0-9_-]{11})\]$", args.video.stem).group(1)
    source = args.work_dir / f"{video_id}.small.en.json"
    cache = args.work_dir / f"{video_id}.zh-CN.progress.json"
    segments = json.loads(source.read_text(encoding="utf-8"))
    for segment in segments:
        for pattern, replacement in REPLACEMENTS:
            segment["text"] = re.sub(pattern, replacement, segment["text"], flags=re.I)
    english = args.video.with_name(args.video.stem + ".en.local.srt")
    chinese = args.video.with_name(args.video.stem + ".zh-CN.srt")
    write_srt(english, segments, "text")
    translations = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else []
    if len(translations) > len(segments):
        raise ValueError("Translation cache exceeds source length")
    for start in range(len(translations), len(segments), 12):
        batch = [segment["text"] for segment in segments[start:start + 12]]
        for attempt in range(5):
            try:
                translated = translate_batch(batch)
                break
            except (urllib.error.URLError, TimeoutError, ValueError):
                if attempt == 4:
                    raise
                time.sleep(3 * (attempt + 1))
        translations.extend(translated)
        cache.write_text(json.dumps(translations, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{video_id}: {len(translations)}/{len(segments)}", flush=True)
        time.sleep(.5)
    for segment, translated in zip(segments, translations):
        for pattern, replacement in REPLACEMENTS:
            translated = re.sub(pattern, replacement, translated, flags=re.I)
        for original, replacement in CHINESE_REPLACEMENTS:
            translated = translated.replace(original, replacement)
        segment["zh"] = translated
    write_srt(chinese, segments, "zh")
    print(f"WROTE {chinese}", flush=True)


if __name__ == "__main__":
    main()
