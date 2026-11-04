"""Build a test zip of N call recordings for the k6 zip-upload test.

Two modes:
  --source-dir DIR   Cycle real recordings (.mp3/.wav/.m4a) from DIR until N files.
                     Recommended: realistic transcription/evaluation timings.
  (no source dir)    Generate synthetic 8 kHz mono WAVs (tone + noise). Fast to build,
                     but transcription/scoring of synthetic audio is NOT representative.

Every entry gets a unique, run-prefixed name: the backend dedups by filename within a
group (it clones the earlier call instead of processing it), which would skew timings.
Entries are STORED (no compression) and there are no directory entries, so the zip stays
inside the backend's limits: <= 5000 members, compression ratio <= 100.

Usage:
  python -I generate_test_zip.py --count 5000 --source-dir D:/qat_recordings --out calls_5000.zip
  python -I generate_test_zip.py --count 5000 --seconds 20 --out calls_5000.zip
"""
import argparse
import io
import math
import random
import struct
import sys
import time
import wave
import zipfile
from pathlib import Path

ALLOWED_EXTS = {".wav", ".mp3", ".m4a"}
MAX_ZIP_MEMBERS = 5000  # mirrors app/api/calls_upload_script/service.py
MAX_ZIP_UNCOMPRESSED_BYTES = 10 * 1024 ** 3


def synth_wav(seconds: int, seed: int, rate: int = 8000) -> bytes:
    """Tone + noise so each file differs and doesn't compress to a zip-bomb ratio."""
    rnd = random.Random(seed)
    freq = rnd.uniform(180, 420)
    frames = bytearray()
    for i in range(seconds * rate):
        s = 0.35 * math.sin(2 * math.pi * freq * i / rate) + rnd.uniform(-0.15, 0.15)
        frames += struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32767))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return buf.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--count", type=int, default=5000)
    ap.add_argument("--out", type=Path, default=Path("calls_5000.zip"))
    ap.add_argument("--source-dir", type=Path, help="folder of real recordings to cycle through")
    ap.add_argument("--seconds", type=int, default=20, help="synthetic clip length (no --source-dir)")
    ap.add_argument("--prefix", default=time.strftime("perf%Y%m%d%H%M%S"), help="unique run prefix for filenames")
    args = ap.parse_args()

    if not 1 <= args.count <= MAX_ZIP_MEMBERS:
        print(f"--count must be 1..{MAX_ZIP_MEMBERS} (backend rejects bigger zips in the background)", file=sys.stderr)
        return 2

    sources = []
    if args.source_dir:
        if not args.source_dir.is_dir():
            print(f"--source-dir not found: {args.source_dir}", file=sys.stderr)
            return 2
        sources = sorted(p for p in args.source_dir.rglob("*") if p.is_file() and p.suffix.lower() in ALLOWED_EXTS)
        if not sources:
            print(f"No .mp3/.wav/.m4a files in {args.source_dir}", file=sys.stderr)
            return 2
        print(f"Cycling {len(sources)} source recordings -> {args.count} entries")

    # Synthetic mode: pre-render a small pool and vary names; content variety isn't needed for dedup.
    synth_pool = [synth_wav(args.seconds, seed) for seed in range(min(25, args.count))] if not sources else []

    total = 0
    started = time.time()
    try:
        with zipfile.ZipFile(args.out, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
            for i in range(args.count):
                if sources:
                    src = sources[i % len(sources)]
                    data = src.read_bytes()
                    ext = src.suffix.lower()
                else:
                    data = synth_pool[i % len(synth_pool)]
                    ext = ".wav"
                total += len(data)
                if total > MAX_ZIP_UNCOMPRESSED_BYTES:
                    raise RuntimeError(f"Uncompressed size passed the backend's 10 GB limit at entry {i}")
                zf.writestr(f"{args.prefix}_{i + 1:05d}{ext}", data)
                if (i + 1) % 500 == 0:
                    print(f"  {i + 1}/{args.count} entries, {total / 1024 ** 2:.0f} MB")
    except (OSError, RuntimeError) as e:
        args.out.unlink(missing_ok=True)
        print(f"Failed to build zip: {e}", file=sys.stderr)
        return 1

    size_mb = args.out.stat().st_size / 1024 ** 2
    print(f"Wrote {args.out} - {args.count} calls, {size_mb:.1f} MB in {time.time() - started:.0f}s")
    if size_mb > 2048:
        print("WARNING: zip > 2 GB. k6 holds it in memory (x2 for the multipart body) and proxies/ALB "
              "may reject or time out. Consider shorter recordings.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
