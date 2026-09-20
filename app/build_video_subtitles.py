import argparse
import json
import os
import re
from pathlib import Path

import torch
import whisper


def stamp(seconds):
    millis = max(0, round(seconds * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    seconds, millis = divmod(millis, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


def write_srt(path, segments, key="text"):
    with path.open("w", encoding="utf-8-sig", newline="\n") as stream:
        for index, segment in enumerate(segments, 1):
            start = segment["start"]
            end = max(start + 0.1, segment["end"])
            text = re.sub(r"\s+", " ", segment[key]).strip()
            stream.write(f"{index}\n{stamp(start)} --> {stamp(end)}\n{text}\n\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("video", type=Path)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--translation-dir", type=Path, required=True)
    parser.add_argument("--ffmpeg-dir", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--whisper-model", default="small.en")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--translate", action="store_true")
    args = parser.parse_args()

    os.environ["PATH"] = str(args.ffmpeg_dir) + os.pathsep + os.environ["PATH"]
    torch.set_num_threads(max(1, min(os.cpu_count() or 4, 8)))
    base = args.video.with_suffix("")
    english_path = base.with_name(base.name + ".en.local.srt")
    args.work_dir.mkdir(parents=True, exist_ok=True)
    match = re.search(r"\[([A-Za-z0-9_-]{11})\]$", base.name)
    video_id = match.group(1) if match else base.name
    json_path = args.work_dir / f"{video_id}.{args.whisper_model}.json"

    if json_path.exists() and not args.force:
        segments = json.loads(json_path.read_text(encoding="utf-8"))
    else:
        model = whisper.load_model(args.whisper_model, download_root=str(args.model_dir), device="cpu")
        result = model.transcribe(
            str(args.video), language="en", fp16=False, verbose=False,
            condition_on_previous_text=False,
        )
        segments = [{"start": s["start"], "end": s["end"], "text": s["text"].strip()}
                    for s in result["segments"] if s["text"].strip()]
        json_path.write_text(json.dumps(segments, ensure_ascii=False, indent=2), encoding="utf-8")
    write_srt(english_path, segments)
    print(f"TRANSCRIBED {args.video.name}: {len(segments)} cues", flush=True)

    if args.translate:
        from transformers import MarianMTModel, MarianTokenizer

        tokenizer = MarianTokenizer.from_pretrained(str(args.translation_dir), local_files_only=True)
        translator = MarianMTModel.from_pretrained(str(args.translation_dir), local_files_only=True).to("cpu")
        translator.eval()
        for start in range(0, len(segments), 16):
            batch = segments[start:start + 16]
            inputs = tokenizer([s["text"] for s in batch], return_tensors="pt", padding=True,
                               truncation=True, max_length=512)
            with torch.no_grad():
                output = translator.generate(**inputs, max_new_tokens=128, num_beams=4)
            for segment, translated in zip(batch, tokenizer.batch_decode(output, skip_special_tokens=True)):
                segment["zh"] = translated
            print(f"TRANSLATED {min(start + 16, len(segments))}/{len(segments)}", flush=True)
        chinese_path = base.with_name(base.name + ".zh-CN.srt")
        write_srt(chinese_path, segments, "zh")
        print(f"WROTE {chinese_path}", flush=True)


if __name__ == "__main__":
    main()
