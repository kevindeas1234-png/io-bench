#!/usr/bin/env python3
"""io_bench - measure how fast an agent can move bytes through a machine.

Compares three data paths over an identical workload:

    buffered   Python read()/write() with no Python-level buffering
    mmap       both files mapped into memory, transform in place
    direct     Windows FILE_FLAG_NO_BUFFERING with sector-aligned buffers
               (bypasses the OS file cache; Windows only)

Every run is archived with a number, a timestamp, hashes and a log, so a
result can be reproduced and audited rather than screenshotted.

Only the standard library is used. Direct I/O needs ctypes and Windows.

    py bench.py                     # 256 MiB, 3 runs each
    py bench.py --size-mib 1024 --runs 5
    py bench.py --list
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import statistics
import sys
import time

try:
    import mmap
except ImportError:          # platforms without mmap simply skip that path
    mmap = None

CHUNK = 1 << 20              # 1 MiB
SECTOR = 4096                # alignment for unbuffered I/O
MAGIC = 0x5A                 # transform key: XOR every byte

# Precomputed translate table keeps the transform inside C, so what we
# measure is the data path, not the Python interpreter.
TABLE = bytes(b ^ MAGIC for b in range(256))

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results")


# ---------------------------------------------------------------------------
# workload
# ---------------------------------------------------------------------------
def make_input(path: str, size: int) -> None:
    """Create a deterministic-but-incompressible file of `size` bytes."""
    if os.path.exists(path) and os.path.getsize(path) == size:
        return
    with open(path, "wb") as handle:
        seed = b"anvil-io-bench"
        written = 0
        digest = hashlib.blake2b(seed).digest()
        while written < size:
            block = hashlib.blake2b(digest).digest() * 32
            take = min(len(block), size - written)
            handle.write(block[:take])
            written += take
            digest = hashlib.blake2b(block).digest()


def verify(path: str) -> str:
    digest = hashlib.blake2b()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()[:32]


# ---------------------------------------------------------------------------
# data paths
# ---------------------------------------------------------------------------
def path_buffered(src: str, dst: str, size: int) -> None:
    with open(src, "rb", buffering=0) as fin, open(dst, "wb", buffering=0) as fout:
        while True:
            chunk = fin.read(CHUNK)
            if not chunk:
                break
            fout.write(chunk.translate(TABLE))


def path_mmap(src: str, dst: str, size: int) -> None:
    if mmap is None:
        raise RuntimeError("mmap not available")
    # Windows requires a read+write handle for ACCESS_WRITE, so "w+b" rather
    # than "wb" - a write-only handle raises WinError 5.
    with open(src, "rb") as fin, open(dst, "w+b") as fout:
        fout.truncate(size)
        with mmap.mmap(fin.fileno(), 0, access=mmap.ACCESS_READ) as mi, \
                mmap.mmap(fout.fileno(), size, access=mmap.ACCESS_WRITE) as mo:
            offset = 0
            while offset < size:
                n = min(CHUNK, size - offset)
                mo[offset:offset + n] = mi[offset:offset + n].translate(TABLE)
                offset += n


def path_direct(src: str, dst: str, size: int) -> None:
    """Windows unbuffered I/O: aligned buffers, cache bypassed."""
    if platform.system() != "Windows":
        raise RuntimeError("direct I/O path is Windows only")
    if size % SECTOR:
        raise RuntimeError("direct path needs a size that is a multiple of %d bytes" % SECTOR)

    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
    OPEN_EXISTING, CREATE_ALWAYS = 3, 2
    FILE_FLAG_NO_BUFFERING = 0x20000000
    FILE_ATTRIBUTE_NORMAL = 0x80
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                wintypes.HANDLE]
    k32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                             ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    k32.WriteFile.argtypes = list(k32.ReadFile.argtypes)
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    fin = k32.CreateFileW(src, GENERIC_READ, 1, None, OPEN_EXISTING,
                          FILE_ATTRIBUTE_NORMAL | FILE_FLAG_NO_BUFFERING, None)
    if fin == INVALID_HANDLE_VALUE:
        raise OSError(ctypes.get_last_error(), "CreateFileW(read) failed")

    fout = k32.CreateFileW(dst, GENERIC_WRITE, 0, None, CREATE_ALWAYS,
                           FILE_ATTRIBUTE_NORMAL | FILE_FLAG_NO_BUFFERING, None)
    if fout == INVALID_HANDLE_VALUE:
        k32.CloseHandle(fin)
        raise OSError(ctypes.get_last_error(), "CreateFileW(write) failed")

    # one aligned staging buffer for both directions
    raw = ctypes.create_string_buffer(CHUNK + SECTOR)
    base = ctypes.addressof(raw)
    aligned = (base + SECTOR - 1) & ~(SECTOR - 1)
    ptr = ctypes.c_void_p(aligned)
    read_bytes = wintypes.DWORD(0)
    written_bytes = wintypes.DWORD(0)

    try:
        remaining = size
        while remaining > 0:
            want = min(CHUNK, remaining) // SECTOR * SECTOR
            if want == 0:
                break
            if not k32.ReadFile(fin, ptr, want, ctypes.byref(read_bytes), None):
                raise OSError(ctypes.get_last_error(), "ReadFile failed")
            got = read_bytes.value
            if got == 0:
                break
            chunk = ctypes.string_at(ptr, got).translate(TABLE)
            ctypes.memmove(ptr, chunk, len(chunk))
            done = 0
            while done < len(chunk):
                if not k32.WriteFile(fout, ctypes.c_void_p(aligned + done),
                                     len(chunk) - done, ctypes.byref(written_bytes), None):
                    raise OSError(ctypes.get_last_error(), "WriteFile failed")
                done += written_bytes.value
            remaining -= got
    finally:
        k32.CloseHandle(fin)
        k32.CloseHandle(fout)


DEPTH = 16                  # outstanding operations for the IOCP path
BATCH = 32 << 20            # phase size for path_phased


def path_iocp(src: str, dst: str, size: int, depth: int | None = None) -> None:
    """Windows unbuffered I/O with `depth` operations in flight via IOCP.

    This is the experiment that matters: the synchronous path is latency-bound,
    so overlapping reads and writes should scale throughput until the device
    saturates. Each slot owns an aligned buffer plus its own OVERLAPPED (and
    therefore its own file offset), so no two operations share state.
    """
    if platform.system() != "Windows":
        raise RuntimeError("iocp path is Windows only")
    if size % SECTOR:
        raise RuntimeError("iocp path needs a sector-aligned size")

    from ctypes import wintypes

    depth = depth or DEPTH
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
    OPEN_EXISTING, CREATE_ALWAYS = 3, 2
    FILE_ATTRIBUTE_NORMAL = 0x80
    FILE_FLAG_NO_BUFFERING = 0x20000000
    FILE_FLAG_OVERLAPPED = 0x40000000
    ERROR_IO_PENDING = 997
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class OVERLAPPED(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_void_p),
            ("InternalHigh", ctypes.c_void_p),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                wintypes.HANDLE]
    k32.CreateIoCompletionPort.restype = wintypes.HANDLE
    k32.CreateIoCompletionPort.argtypes = [wintypes.HANDLE, wintypes.HANDLE,
                                           ctypes.c_size_t, wintypes.DWORD]
    k32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                             ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(OVERLAPPED)]
    k32.WriteFile.argtypes = list(k32.ReadFile.argtypes)
    k32.GetQueuedCompletionStatus.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.c_size_t),
        ctypes.POINTER(ctypes.POINTER(OVERLAPPED)), wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    flags = FILE_ATTRIBUTE_NORMAL | FILE_FLAG_NO_BUFFERING | FILE_FLAG_OVERLAPPED
    fin = k32.CreateFileW(src, GENERIC_READ, 1, None, OPEN_EXISTING, flags, None)
    if fin == INVALID_HANDLE_VALUE:
        raise OSError(ctypes.get_last_error(), "CreateFileW(read)")
    fout = k32.CreateFileW(dst, GENERIC_WRITE, 0, None, CREATE_ALWAYS, flags, None)
    if fout == INVALID_HANDLE_VALUE:
        k32.CloseHandle(fin)
        raise OSError(ctypes.get_last_error(), "CreateFileW(write)")

    iocp = k32.CreateIoCompletionPort(INVALID_HANDLE_VALUE, None, 0, 0)
    k32.CreateIoCompletionPort(fin, iocp, 1, 0)     # key 1 = read handle
    k32.CreateIoCompletionPort(fout, iocp, 2, 0)    # key 2 = write handle

    slots = []
    for _ in range(depth):
        raw = ctypes.create_string_buffer(CHUNK + SECTOR)
        aligned = (ctypes.addressof(raw) + SECTOR - 1) & ~(SECTOR - 1)
        slots.append({"raw": raw, "ptr": aligned, "ov": OVERLAPPED(),
                      "offset": 0, "length": 0, "state": "idle"})
    by_ov = {ctypes.addressof(slot["ov"]): slot for slot in slots}

    def begin_read(slot, offset):
        length = min(CHUNK, size - offset)
        slot["offset"] = offset
        slot["length"] = length
        slot["state"] = "read"
        slot["ov"].Offset = offset & 0xFFFFFFFF
        slot["ov"].OffsetHigh = (offset >> 32) & 0xFFFFFFFF
        if not k32.ReadFile(fin, ctypes.c_void_p(slot["ptr"]), length, None,
                            ctypes.byref(slot["ov"])):
            err = ctypes.get_last_error()
            if err != ERROR_IO_PENDING:
                raise OSError(err, "ReadFile")

    def begin_write(slot):
        slot["state"] = "write"
        if not k32.WriteFile(fout, ctypes.c_void_p(slot["ptr"]), slot["length"], None,
                             ctypes.byref(slot["ov"])):
            err = ctypes.get_last_error()
            if err != ERROR_IO_PENDING:
                raise OSError(err, "WriteFile")

    next_offset = 0
    pending = 0
    for slot in slots:
        if next_offset >= size:
            break
        begin_read(slot, next_offset)
        next_offset += slot["length"]
        pending += 1

    try:
        while pending:
            transferred = wintypes.DWORD(0)
            key = ctypes.c_size_t(0)
            ovp = ctypes.POINTER(OVERLAPPED)()
            k32.GetQueuedCompletionStatus(iocp, ctypes.byref(transferred), ctypes.byref(key),
                                          ctypes.byref(ovp), 30000)
            if not ovp:
                raise OSError(ctypes.get_last_error(), "GetQueuedCompletionStatus")

            slot = by_ov[ctypes.addressof(ovp.contents)]
            if slot["state"] == "read":
                chunk = ctypes.string_at(slot["ptr"], transferred.value).translate(TABLE)
                ctypes.memmove(slot["ptr"], chunk, len(chunk))
                begin_write(slot)
            else:
                slot["state"] = "idle"
                if next_offset < size:
                    begin_read(slot, next_offset)
                    next_offset += slot["length"]
                else:
                    pending -= 1
    finally:
        k32.CloseHandle(fout)
        k32.CloseHandle(fin)
        k32.CloseHandle(iocp)


def path_phased(src: str, dst: str, size: int) -> None:
    """Read a whole batch into memory, then write it out.

    On a spinning disk, alternating read and write per chunk makes the head
    swing between two regions. Doing it in large batches means long sequential
    runs instead. Same bytes, same output, fewer direction changes.
    """
    with open(src, "rb", buffering=0) as fin, open(dst, "wb", buffering=0) as fout:
        remaining = size
        while remaining > 0:
            want = min(BATCH, remaining)
            data = b""
            while len(data) < want:
                chunk = fin.read(min(CHUNK, want - len(data)))
                if not chunk:
                    break
                data += chunk
            if not data:
                break
            fout.write(data.translate(TABLE))
            remaining -= len(data)


PATHWAYS = {
    "buffered": path_buffered,
    "phased": path_phased,
    "mmap": path_mmap,
    "direct": path_direct,
    "iocp": path_iocp,
}


# ---------------------------------------------------------------------------
# measurement
# ---------------------------------------------------------------------------
def measure(method: str, func, src: str, dst: str, size: int, runs: int) -> dict:
    results = []
    digest = None
    for index in range(runs + 1):          # first pass is a warm-up
        start = time.perf_counter()
        func(src, dst, size)
        elapsed = time.perf_counter() - start
        if index == 0:
            digest = verify(dst)           # correctness gate
            continue
        results.append(elapsed)
    median = statistics.median(results)
    return {
        "method": method,
        "runs_s": [round(r, 4) for r in results],
        "median_s": round(median, 4),
        "mib_per_s": round((size / (1024 * 1024)) / median, 2),
        "output_hash": digest,
    }


def next_run_id(base: str) -> int:
    numbers = [0]
    if os.path.isdir(base):
        for entry in os.listdir(base):
            if entry.startswith("run_"):
                try:
                    numbers.append(int(entry.split("_")[1]))
                except (IndexError, ValueError):
                    pass
    return max(numbers) + 1


def archive(meta: dict, transcript: str) -> str:
    run_id = next_run_id(RESULTS)
    run_dir = os.path.join(RESULTS, f"run_{run_id:03d}_{time.strftime('%Y-%m-%d_%H%M%S')}")
    os.makedirs(run_dir, exist_ok=True)

    with open(os.path.join(run_dir, "meta.json"), "w", encoding="utf-8") as handle:
        json.dump(meta, handle, indent=2)
    with open(os.path.join(run_dir, "bench.log"), "w", encoding="utf-8") as handle:
        handle.write(transcript)

    catalogue = os.path.join(RESULTS, "runs.json")
    entries = []
    if os.path.isfile(catalogue):
        try:
            with open(catalogue, encoding="utf-8") as handle:
                entries = json.load(handle)
        except (OSError, ValueError):
            entries = []
    entries.append({
        "run": run_id,
        "started": meta["started"],
        "size_mib": meta["size_mib"],
        "methods": {m["method"]: m["mib_per_s"] for m in meta["methods"] if "mib_per_s" in m},
    })
    with open(catalogue, "w", encoding="utf-8") as handle:
        json.dump(entries, handle, indent=2)
    return run_dir


def list_runs() -> int:
    catalogue = os.path.join(RESULTS, "runs.json")
    if not os.path.isfile(catalogue):
        print("no runs recorded yet")
        return 0
    with open(catalogue, encoding="utf-8") as handle:
        entries = json.load(handle)
    print(f"{'RUN':>4}  {'STARTED':<19}  {'MiB':>5}  METHODS (MiB/s)")
    for entry in entries:
        speeds = ", ".join(f"{k}={v}" for k, v in entry["methods"].items())
        print(f"{entry['run']:>4}  {entry['started']:<19}  {entry['size_mib']:>5}  {speeds}")
    return 0


# ---------------------------------------------------------------------------
def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Compare data paths over one workload.")
    parser.add_argument("--size-mib", type=int, default=256)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--methods", default="buffered,mmap,direct,iocp")
    parser.add_argument("--depths", default="1,4,16,64",
                        help="queue depths to sweep for the iocp path")
    parser.add_argument("--depth", type=int, default=DEPTH)
    parser.add_argument("--chunk-kib", type=int, default=1024,
                        help="I/O size per operation")
    parser.add_argument("--batch-mib", type=int, default=32,
                        help="phase size for the phased path")
    parser.add_argument("--workdir", default=os.path.join(HERE, "work"))
    parser.add_argument("--dst-workdir", default=None,
                        help="write outputs here instead (e.g. a second disk)")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)

    if args.list:
        return list_runs()

    global CHUNK, BATCH
    CHUNK = args.chunk_kib * 1024
    BATCH = args.batch_mib * 1024 * 1024

    size = args.size_mib * 1024 * 1024
    methods = [m.strip() for m in args.methods.split(",") if m.strip()]
    depths = [int(d) for d in args.depths.split(",") if d.strip()]

    # expand "iocp" into one job per queue depth so the scaling is visible
    jobs = []
    for name in methods:
        if name == "iocp":
            for depth in depths:
                jobs.append((f"iocp@{depth}", lambda s, d, z, n=depth: path_iocp(s, d, z, n)))
        else:
            jobs.append((name, PATHWAYS[name]))

    os.makedirs(args.workdir, exist_ok=True)

    outdir = args.dst_workdir or args.workdir
    os.makedirs(outdir, exist_ok=True)

    src = os.path.join(args.workdir, "input.bin")
    print(f"preparing {args.size_mib} MiB workload in {args.workdir}")
    if outdir != args.workdir:
        print(f"writing outputs to {outdir}")
    make_input(src, size)

    transcript_lines = []
    def say(line: str = "") -> None:
        print(line)
        transcript_lines.append(line)

    say(f"io_bench  python {platform.python_version()}  {platform.platform()}")
    say(f"workload  {args.size_mib} MiB  runs={args.runs} (+1 warm-up)  "
        f"chunk={CHUNK >> 10} KiB  batch={BATCH >> 20} MiB")
    say("")

    results = []
    hashes = set()
    for method, func in jobs:
        dst = os.path.join(outdir, f"out_{method.replace('@', '_')}.bin")
        try:
            result = measure(method, func, src, dst, size, args.runs)
        except Exception as exc:                      # noqa: BLE001 - report, never crash
            say(f"{method:<9} unavailable: {exc}")
            results.append({"method": method, "error": str(exc)})
            continue
        results.append(result)
        hashes.add(result["output_hash"])
        say(f"{method:<9} median {result['median_s']:6.3f} s   "
            f"{result['mib_per_s']:8.2f} MiB/s   runs={result['runs_s']}")

    say("")
    if len(hashes) > 1:
        say("WARNING: outputs differ between methods - results are not comparable")
    elif hashes:
        say(f"all methods produced identical output ({next(iter(hashes))})")
    else:
        say("no method completed")

    ok = [r for r in results if "mib_per_s" in r]
    if ok:
        best = max(ok, key=lambda r: r["mib_per_s"])
        say(f"fastest: {best['method']} at {best['mib_per_s']} MiB/s")

    meta = {
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "size_mib": args.size_mib,
        "runs": args.runs,
        "chunk_bytes": CHUNK,
        "methods": results,
        "script_sha256": hashlib.sha256(open(os.path.abspath(__file__), "rb").read()).hexdigest(),
    }
    run_dir = archive(meta, "\n".join(transcript_lines) + "\n")
    print(f"\narchived: {run_dir}")

    return 0 if len(hashes) <= 1 and ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
