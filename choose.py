#!/usr/bin/env python3
"""Probe, decide, then write - the policy the benchmarks argue for.

The measurements in this repo say the same thing from four directions:

    queue depth      does not help once the device saturates
    in-place rewrite worse than copying (read-modify-write serialises)
    compressible    3.6x faster, 64x fewer bytes written
    incompressible  0.17x speed for nothing

So the choice that matters is not *how* to issue the writes, it is *how many
bytes* to write. This script makes that choice automatically: sample the front
of the input, compress it, and keep the compressed form only if it actually
saves enough to be worth the CPU.

    py choose.py --src bigfile.bin --dst bigfile.anvil --direct
    py choose.py --src bigfile.bin --dst out.bin --min-saving 0.2

The decision and its evidence are printed, and the output is verified by
round-tripping it back to the source hash.
"""

from __future__ import annotations

import argparse
import os
import platform
import statistics
import sys
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import compress as c          # reuse the paths already benchmarked

SAMPLE_BYTES = 4 << 20        # probe the first 4 MiB


def compressibility(path: str, level: int, sample: int = SAMPLE_BYTES) -> float:
    """Return compressed/original for a sample. Below 1.0 means it pays."""
    with open(path, "rb") as handle:
        data = handle.read(sample)
    if not data:
        return 1.0
    packed = zlib.compress(data, level)
    return len(packed) / len(data)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="probe then decide")
    parser.add_argument("--src", required=True)
    parser.add_argument("--dst", required=True)
    parser.add_argument("--level", type=int, default=1)
    parser.add_argument("--min-saving", type=float, default=0.1,
                        help="fraction of bytes that must be saved to compress")
    parser.add_argument("--direct", action="store_true")
    parser.add_argument("--runs", type=int, default=1)
    args = parser.parse_args(argv)

    size = os.path.getsize(args.src)
    if size == 0:
        print("input is empty")
        return 2

    ratio = compressibility(args.src, args.level)
    threshold = 1.0 - args.min_saving
    use_packed = ratio < threshold

    print(f"io_bench choose  python {platform.python_version()}  {platform.platform()}")
    print(f"input   {args.src}  ({size / (1024 * 1024):.1f} MiB)")
    print(f"probe   {SAMPLE_BYTES >> 20} MiB -> compressed ratio {ratio:.3f} "
          f"({(1 - ratio) * 100:.0f}% smaller)")
    print(f"policy  threshold {threshold:.2f} -> "
          f"{'COMPRESS' if use_packed else 'WRITE PLAIN'}")
    print(f"mode    {'direct' if args.direct else 'buffered'}\n")

    if use_packed:
        action = lambda: c.run_packed(args.src, args.dst, size, args.level, args.direct)
    else:
        action = lambda: c.run_plain(args.src, args.dst, size, args.direct)

    times = []
    for index in range(args.runs + 1):
        start = time.perf_counter()
        action()
        elapsed = time.perf_counter() - start
        if index:
            times.append(elapsed)

    median = statistics.median(times)
    written = os.path.getsize(args.dst)
    print(f"\nmedian {median:6.3f} s   payload {(size / (1024 * 1024)) / median:8.2f} MiB/s")
    print(f"wrote  {written / (1024 * 1024):7.1f} MiB  "
          f"({written / size:.2f}x the input)")

    expected = c.hash_file(args.src)
    if use_packed:
        got, recovered = c.unpack_hash(args.dst)
    else:
        got, recovered = c.hash_file(args.dst), size
    ok = got == expected
    print(f"verify {'OK' if ok else 'WRONG'} ({recovered} bytes recovered)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
