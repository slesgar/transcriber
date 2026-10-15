#!/usr/bin/env python3
"""mlx-whisper на тех же фиксированных блоках, что и wkspike — чтобы сравнивать текст, а не нарезку.

Читает int16 PCM, режет ровно так же (фиксированные блоки без перекрытия), зовёт
mlx_whisper.transcribe с теми же language/prompt/temperature, пишет такой же JSON.
Воркер не импортируется и не правится — вызов mlx_whisper здесь прямой.

  uv run python mlx_blocks.py <pcm> --model <repo> [--prompt T] [--block-s 60]
                              [--offset-s N] [--limit-s N] --out <json>
"""
from __future__ import annotations

import argparse
import json
import time

import numpy as np

SR = 16000


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pcm")
    ap.add_argument("--model", default="mlx-community/whisper-large-v3-turbo")
    ap.add_argument("--prompt", default="")
    ap.add_argument("--block-s", type=float, default=60.0)
    ap.add_argument("--offset-s", type=float, default=0.0)
    ap.add_argument("--limit-s", type=float, default=0.0)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    import mlx_whisper

    raw = np.memmap(a.pcm, dtype=np.int16, mode="r")
    start = min(int(a.offset_s * SR), len(raw))
    count = min(int(a.limit_s * SR), len(raw) - start) if a.limit_s > 0 else len(raw) - start
    audio = np.asarray(raw[start:start + count], dtype=np.float32) / 32768.0
    audio_seconds = len(audio) / SR
    print(f"pcm: {a.pcm} {audio_seconds:.1f}s from {a.offset_s}s", flush=True)

    block_samples = int(a.block_s * SR)
    segments, blocks = [], []
    run_start = time.monotonic()
    pos, idx = 0, 0
    while pos < len(audio):
        end = min(pos + block_samples, len(audio))
        chunk = np.ascontiguousarray(audio[pos:end], dtype=np.float32)
        block_start = pos / SR + a.offset_s
        t0 = time.monotonic()
        result = mlx_whisper.transcribe(
            chunk,
            path_or_hf_repo=a.model,
            language="ru",
            initial_prompt=a.prompt or None,
            temperature=0.0,
            fp16=True,
            condition_on_previous_text=True,
        )
        wall = time.monotonic() - t0
        n = 0
        for s in result.get("segments") or []:
            text = (s.get("text") or "").strip()
            if not text:
                continue
            segments.append({
                "start": block_start + float(s.get("start", 0.0) or 0.0),
                "end": block_start + float(s.get("end", 0.0) or 0.0),
                "text": text,
                "block": idx,
                "avgLogprob": float(s.get("avg_logprob", 0.0) or 0.0),
                "noSpeechProb": float(s.get("no_speech_prob", 0.0) or 0.0),
                "compressionRatio": float(s.get("compression_ratio", 0.0) or 0.0),
                "temperature": float(s.get("temperature", 0.0) or 0.0),
            })
            n += 1
        blocks.append({"index": idx, "start": block_start, "end": end / SR + a.offset_s,
                       "wallSeconds": wall, "segments": n})
        print(f"block {idx} {block_start:.0f}-{end / SR + a.offset_s:.0f}s -> {n} seg in {wall:.1f}s", flush=True)
        idx += 1
        pos = end

    wall_seconds = time.monotonic() - run_start
    out = {
        "model": a.model,
        "modelFolder": a.model,
        "prompt": a.prompt,
        "promptTokens": 0,
        "blockSeconds": a.block_s,
        "offsetSeconds": a.offset_s,
        "audioSeconds": audio_seconds,
        "wallSeconds": wall_seconds,
        "loadSeconds": 0.0,
        "realtimeFactor": audio_seconds / wall_seconds if wall_seconds else 0.0,
        "computeUnits": {"backend": "mlx (GPU/Metal)"},
        "blocks": blocks,
        "segments": segments,
    }
    with open(a.out, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"wrote {a.out}: {len(segments)} segments, {wall_seconds:.1f}s wall, "
          f"x{out['realtimeFactor']:.2f} realtime", flush=True)


if __name__ == "__main__":
    main()
