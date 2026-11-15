#!/usr/bin/env python3
"""Does writing fewer bytes actually help on a write-slow device?

The previous experiments ruled out queue depth, locality and file count. This
one tests the remaining lever: write less.

Two shapes, identical read side, cache bypassed on the write side (the side
that binds on a read-fast/write-slow drive):

    plain      read 1 MiB -> write 1 MiB
    packed     read 1 MiB -> zlib -> write one framed record
               (header + compressed payload, zero-padded to a sector)

Records are padded to 4096 bytes so the writer can use FILE_FLAG_NO_BUFFERING.
Verification decompresses the archive and compares hashes with the source, so
a faster-but-wrong result cannot pass.

    py compress.py --workdir C:\\Users\\kevin\\io_bench_work --data text --level 1 --direct
    py compress.py --workdir C:\\Users\\kevin\\io_bench_work --data random --level 1 --direct
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import os
import platform
import statistics
import struct
import time
import zlib

CHUNK = 1 << 20
SECTOR = 4096
HEADER = 8                      # u32 original length + u32 compressed length


# ---------------------------------------------------------------------------
# workloads: one that compresses, one that cannot
# ---------------------------------------------------------------------------
def make_random(path: str, size: int) -> None:
    """Genuinely incompressible data - os.urandom, not a repeated digest."""
    if os.path.exists(path) and os.path.getsize(path) == size:
        return
    with open(path, "wb") as handle:
        written = 0
        while written < size:
            block = os.urandom(min(CHUNK, size - written))
            handle.write(block)
            written += len(block)


def make_text(path: str, size: int) -> None:
    if os.path.exists(path) and os.path.getsize(path) == size:
        return
    unit = (
        "anvil telemetry record 42: device=nvme0 state=ok latency_us=118 "
        "queue=32 bytes=1048576 status=complete\n"
    ).encode()
    block = unit * (CHUNK // len(unit) + 1)
    block = block[:CHUNK]
    with open(path, "wb") as handle:
        written = 0
        while written < size:
            take = min(len(block), size - written)
            handle.write(block[:take])
            written += take


def hash_file(path: str) -> str:
    digest = hashlib.blake2b()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# writers: buffered fallback, and cache-bypassing on Windows
# ---------------------------------------------------------------------------
class BufferedWriter:
    def __init__(self, path: str):
        self.handle = open(path, "wb", buffering=0)

    def write(self, data: bytes) -> None:
        self.handle.write(data)

    def close(self) -> None:
        self.handle.close()


class DirectWriter:
    """FILE_FLAG_NO_BUFFERING: sector-aligned buffers, sector-multiple writes."""

    def __init__(self, path: str):
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                    wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                    wintypes.HANDLE]
        k32.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                                  ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        self.k32 = k32
        self.wintypes = wintypes
        GENERIC_WRITE = 0x40000000
        CREATE_ALWAYS = 2
        FILE_ATTRIBUTE_NORMAL = 0x80
        FILE_FLAG_NO_BUFFERING = 0x20000000
        INVALID = ctypes.c_void_p(-1).value

        self.handle = k32.CreateFileW(path, GENERIC_WRITE, 0, None, CREATE_ALWAYS,
                                      FILE_ATTRIBUTE_NORMAL | FILE_FLAG_NO_BUFFERING, None)
        if self.handle == INVALID:
            raise OSError(ctypes.get_last_error(), "CreateFileW(write)")
        self.raw = ctypes.create_string_buffer(CHUNK + SECTOR * 2)
        self.ptr = (ctypes.addressof(self.raw) + SECTOR - 1) & ~(SECTOR - 1)
        self.capacity = (ctypes.addressof(self.raw) + len(self.raw)) - self.ptr
        self.written = wintypes.DWORD(0)

    def write(self, data: bytes) -> None:
        assert len(data) % SECTOR == 0, "direct writes must be sector multiples"
        if len(data) > self.capacity:
            # incompressible input can expand slightly after framing; grow
            self.raw = ctypes.create_string_buffer(len(data) + SECTOR)
            self.ptr = (ctypes.addressof(self.raw) + SECTOR - 1) & ~(SECTOR - 1)
            self.capacity = (ctypes.addressof(self.raw) + len(self.raw)) - self.ptr
        ctypes.memmove(self.ptr, data, len(data))
        offset = 0
        while offset < len(data):
            if not self.k32.WriteFile(self.handle, ctypes.c_void_p(self.ptr + offset),
                                      len(data) - offset, ctypes.byref(self.written), None):
                raise OSError(ctypes.get_last_error(), "WriteFile")
            offset += self.written.value

    def close(self) -> None:
        self.k32.CloseHandle(self.handle)


def make_writer(path: str, direct: bool):
    if direct:
        if platform.system() != "Windows":
            raise RuntimeError("--direct is Windows only")
        return DirectWriter(path)
    return BufferedWriter(path)


# ---------------------------------------------------------------------------
# shapes
# ---------------------------------------------------------------------------
def run_plain(src: str, dst: str, size: int, direct: bool) -> None:
    writer = make_writer(dst, direct)
    try:
        with open(src, "rb", buffering=0) as fin:
            remaining = size
            while remaining > 0:
                chunk = fin.read(min(CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                if direct:
                    pad = (-len(chunk)) % SECTOR
                    if pad:
                        chunk += bytes(pad)
                writer.write(chunk)
    finally:
        writer.close()


def frame(chunk: bytes, level: int) -> bytes:
    packed = zlib.compress(chunk, level)
    record = struct.pack("<II", len(chunk), len(packed)) + packed
    pad = (-len(record)) % SECTOR
    return record + bytes(pad)


def run_packed(src: str, dst: str, size: int, level: int, direct: bool) -> None:
    writer = make_writer(dst, direct)
    try:
        with open(src, "rb", buffering=0) as fin:
            remaining = size
            while remaining > 0:
                chunk = fin.read(min(CHUNK, remaining))
                if not chunk:
                    break
                writer.write(frame(chunk, level))
                remaining -= len(chunk)
    finally:
        writer.close()


def unpack_hash(path: str) -> tuple[str, int]:
    """Decompress a framed archive; return (hash of payload, payload bytes)."""
    digest = hashlib.blake2b()
    total = 0
    with open(path, "rb") as handle:
        while True:
            header = handle.read(HEADER)
            if not header:
                break
            if len(header) < HEADER:
                break
            orig, packed = struct.unpack("<II", header)
            data = handle.read(packed)
            digest.update(zlib.decompress(data))
            total += orig
            # skip the padding that brought the record up to a sector
            used = HEADER + packed
            pad = (-used) % SECTOR
            if pad:
                handle.seek(pad, os.SEEK_CUR)
    return digest.hexdigest(), total


def measure(func, runs: int) -> list[float]:
    times = []
    for index in range(runs + 1):
        start = time.perf_counter()
        func()
        elapsed = time.perf_counter() - start
        if index:
            times.append(elapsed)
    return times


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="write less, see if it helps")
    parser.add_argument("--size-mib", type=int, default=256)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--level", type=int, default=1, help="zlib level (1 is fast)")
    parser.add_argument("--data", choices=("text", "random"), default="text")
    parser.add_argument("--direct", action="store_true")
    parser.add_argument("--workdir", default=os.path.join(os.path.dirname(
        os.path.abspath(__file__)), "work"))
    args = parser.parse_args(argv)

    size = args.size_mib * 1024 * 1024
    os.makedirs(args.workdir, exist_ok=True)
    src = os.path.join(args.workdir, f"input_{args.data}.bin")
    plain_out = os.path.join(args.workdir, f"plain_{args.data}.bin")
    packed_out = os.path.join(args.workdir, f"packed_{args.data}.bin")

    print(f"io_bench compress  python {platform.python_version()}  {platform.platform()}")
    print(f"workload {args.size_mib} MiB  data={args.data}  level={args.level}  "
          f"mode={'direct' if args.direct else 'buffered'}\n")

    if args.data == "text":
        make_text(src, size)
    else:
        make_random(src, size)

    plain_times = measure(lambda: run_plain(src, plain_out, size, args.direct), args.runs)
    packed_times = measure(lambda: run_packed(src, packed_out, size, args.level, args.direct),
                           args.runs)

    plain_median = statistics.median(plain_times)
    packed_median = statistics.median(packed_times)
    payload_mib = size / (1024 * 1024)
    plain_bytes = os.path.getsize(plain_out) / (1024 * 1024)
    packed_bytes = os.path.getsize(packed_out) / (1024 * 1024)

    print()
    print(f"{'plain':<8} median {plain_median:6.3f} s   payload {payload_mib / plain_median:8.2f} MiB/s   "
          f"written {plain_bytes:7.1f} MiB")
    print(f"{'packed':<8} median {packed_median:6.3f} s   payload {payload_mib / packed_median:8.2f} MiB/s   "
          f"written {packed_bytes:7.1f} MiB")

    expected = hash_file(src)
    got_plain = hash_file(plain_out)
    got_packed, unpacked_bytes = unpack_hash(packed_out)

    print()
    print(f"  plain  output {'OK' if got_plain == expected else 'WRONG'}")
    print(f"  packed output {'OK' if got_packed == expected else 'WRONG'} "
          f"({unpacked_bytes} bytes recovered)")
    print(f"\n  writes reduced by {plain_bytes / packed_bytes:.1f}x, "
          f"packed is {plain_median / packed_median:.2f}x the plain speed")

    return 0 if (got_plain == expected and got_packed == expected) else 1


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
