#!/usr/bin/env python3
"""Does halving the traffic double payload throughput?

A focused experiment, separate from the main harness so the comparison is
clean: identical workload, identical chunk size, two shapes.

    copy      read input.bin  -> write output.bin      (2x device traffic)
    inplace   read work.bin   -> write the same bytes back at the same offset
                               (1x payload, still 2x traffic, but only ONE
                                file region is touched)

The hypothesis from the io_bench results: on a drive that reads fast and
writes slowly, a copy is write-bound, so removing the second file should not
help as much as removing the *write*. What actually helps is not rewriting at
all - which is why the numbers here are worth measuring rather than assuming.

Verification: the in-place transform is idempotent (`b | 1`), so repeated
passes are stable and the final hash is predictable.

    py inplace.py --workdir C:\\Users\\kevin\\io_bench_work --size-mib 256 --runs 3
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import statistics
import time

CHUNK = 1 << 20
SECTOR = 4096                                    # alignment for direct I/O
TABLE_OR = bytes(b | 1 for b in range(256))      # idempotent
TABLE_XOR = bytes(b ^ 0x5A for b in range(256))  # same as io_bench.py


def make_input(path: str, size: int) -> None:
    if os.path.exists(path) and os.path.getsize(path) == size:
        return
    with open(path, "wb") as handle:
        digest = hashlib.blake2b(b"anvil-io-bench").digest()
        written = 0
        while written < size:
            block = digest * 32
            take = min(len(block), size - written)
            handle.write(block[:take])
            written += take
            digest = hashlib.blake2b(block).digest()


def hash_file(path: str) -> str:
    digest = hashlib.blake2b()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def hash_transformed(path: str, table: bytes) -> str:
    digest = hashlib.blake2b()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk.translate(table))
    return digest.hexdigest()


def copy_pass(src: str, dst: str, size: int) -> None:
    with open(src, "rb", buffering=0) as fin, open(dst, "wb", buffering=0) as fout:
        while True:
            chunk = fin.read(CHUNK)
            if not chunk:
                break
            fout.write(chunk.translate(TABLE_XOR))


def inplace_pass(path: str, size: int) -> None:
    with open(path, "r+b", buffering=0) as handle:
        offset = 0
        while offset < size:
            handle.seek(offset)
            chunk = handle.read(CHUNK)
            if not chunk:
                break
            handle.seek(offset)
            handle.write(chunk.translate(TABLE_OR))
            offset += len(chunk)


def _win32_handle(path: str, write: bool):
    """Open a file bypassing the cache, sector-aligned access only."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
    OPEN_EXISTING, CREATE_ALWAYS = 3, 2
    FILE_ATTRIBUTE_NORMAL = 0x80
    FILE_FLAG_NO_BUFFERING = 0x20000000
    INVALID = ctypes.c_void_p(-1).value

    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                wintypes.HANDLE]
    access = GENERIC_WRITE if write else GENERIC_READ
    disposition = CREATE_ALWAYS if write else OPEN_EXISTING
    share = 0 if write else 1
    handle = k32.CreateFileW(path, access, share, None, disposition,
                             FILE_ATTRIBUTE_NORMAL | FILE_FLAG_NO_BUFFERING, None)
    if handle == INVALID:
        raise OSError(ctypes.get_last_error(), f"CreateFileW({path})")
    return k32, handle


def _aligned(size: int):
    import ctypes
    raw = ctypes.create_string_buffer(size + SECTOR)
    return raw, (ctypes.addressof(raw) + SECTOR - 1) & ~(SECTOR - 1)


def copy_direct(src: str, dst: str, size: int) -> None:
    """Copy with the cache bypassed, one operation at a time."""
    import ctypes
    from ctypes import wintypes

    k32r, fin = _win32_handle(src, write=False)
    k32w, fout = _win32_handle(dst, write=True)
    raw, ptr = _aligned(CHUNK)
    read_n = wintypes.DWORD(0)
    written = wintypes.DWORD(0)
    try:
        remaining = size
        while remaining > 0:
            want = min(CHUNK, remaining) // SECTOR * SECTOR
            if want == 0:
                break
            if not k32r.ReadFile(fin, ctypes.c_void_p(ptr), want, ctypes.byref(read_n), None):
                raise OSError(ctypes.get_last_error(), "ReadFile")
            got = read_n.value
            if got == 0:
                break
            chunk = ctypes.string_at(ptr, got).translate(TABLE_XOR)
            ctypes.memmove(ptr, chunk, len(chunk))
            done = 0
            while done < len(chunk):
                if not k32w.WriteFile(fout, ctypes.c_void_p(ptr + done),
                                      len(chunk) - done, ctypes.byref(written), None):
                    raise OSError(ctypes.get_last_error(), "WriteFile")
                done += written.value
            remaining -= got
    finally:
        k32r.CloseHandle(fin)
        k32w.CloseHandle(fout)


def inplace_direct(path: str, size: int) -> None:
    """Read and rewrite the same region with the cache bypassed."""
    import ctypes
    from ctypes import wintypes

    k32, handle = _win32_handle(path, write=False)
    # GENERIC_READ alone cannot write; reopen read+write without truncation
    k32.CloseHandle(handle)
    GENERIC_READ_WRITE = 0x80000000 | 0x40000000
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x80
    FILE_FLAG_NO_BUFFERING = 0x20000000
    INVALID = ctypes.c_void_p(-1).value
    k32.CreateFileW.restype = wintypes.HANDLE
    handle = k32.CreateFileW(path, GENERIC_READ_WRITE, 0, None, OPEN_EXISTING,
                             FILE_ATTRIBUTE_NORMAL | FILE_FLAG_NO_BUFFERING, None)
    if handle == INVALID:
        raise OSError(ctypes.get_last_error(), "CreateFileW(inplace)")

    raw, ptr = _aligned(CHUNK)
    read_n = wintypes.DWORD(0)
    written = wintypes.DWORD(0)

    k32.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong,
                                     ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD]
    try:
        offset = 0
        while offset < size:
            want = min(CHUNK, size - offset) // SECTOR * SECTOR
            if want == 0:
                break
            if not k32.SetFilePointerEx(handle, offset, None, 0):
                raise OSError(ctypes.get_last_error(), "SetFilePointerEx(seek read)")
            if not k32.ReadFile(handle, ctypes.c_void_p(ptr), want, ctypes.byref(read_n), None):
                raise OSError(ctypes.get_last_error(), "ReadFile")
            got = read_n.value
            if got == 0:
                break
            chunk = ctypes.string_at(ptr, got).translate(TABLE_OR)
            ctypes.memmove(ptr, chunk, len(chunk))
            if not k32.SetFilePointerEx(handle, offset, None, 0):
                raise OSError(ctypes.get_last_error(), "SetFilePointerEx(seek write)")
            done = 0
            while done < len(chunk):
                if not k32.WriteFile(handle, ctypes.c_void_p(ptr + done),
                                     len(chunk) - done, ctypes.byref(written), None):
                    raise OSError(ctypes.get_last_error(), "WriteFile")
                done += written.value
            offset += got
    finally:
        k32.CloseHandle(handle)


def measure(func, runs: int) -> list[float]:
    times = []
    for index in range(runs + 1):                 # first pass is warm-up
        start = time.perf_counter()
        func()
        elapsed = time.perf_counter() - start
        if index:
            times.append(elapsed)
    return times


def report(label: str, times: list[float], size: int) -> float:
    median = statistics.median(times)
    payload = (size / (1024 * 1024)) / median
    device = payload * 2                          # read + write
    print(f"{label:<9} median {median:6.3f} s   payload {payload:8.2f} MiB/s   "
          f"device {device:8.2f} MiB/s   runs={[round(t, 3) for t in times]}")
    return payload


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="copy vs in-place")
    parser.add_argument("--size-mib", type=int, default=256)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--workdir", default=os.path.join(os.path.dirname(
        os.path.abspath(__file__)), "work"))
    parser.add_argument("--direct", action="store_true",
                        help="bypass the page cache (Windows); the only honest number")
    args = parser.parse_args(argv)

    size = args.size_mib * 1024 * 1024
    os.makedirs(args.workdir, exist_ok=True)

    src = os.path.join(args.workdir, "input.bin")
    out = os.path.join(args.workdir, "copy_out.bin")
    work = os.path.join(args.workdir, "inplace_work.bin")

    print(f"io_bench inplace  python {platform.python_version()}  {platform.platform()}")
    print(f"workload {args.size_mib} MiB  chunk {CHUNK >> 10} KiB  runs={args.runs}\n")

    make_input(src, size)

    # a pristine working copy for the in-place runs (untimed)
    shutil.copyfile(src, work)

    if args.direct:
        if platform.system() != "Windows":
            print("--direct is Windows only")
            return 2
        print("mode: direct (cache bypassed)\n")
        copy_times = measure(lambda: copy_direct(src, out, size), args.runs)
        inplace_times = measure(lambda: inplace_direct(work, size), args.runs)
    else:
        print("mode: buffered (page cache - measures RAM, not the device)\n")
        copy_times = measure(lambda: copy_pass(src, out, size), args.runs)
        inplace_times = measure(lambda: inplace_pass(work, size), args.runs)

    print()
    copy_payload = report("copy", copy_times, size)
    inplace_payload = report("inplace", inplace_times, size)

    # correctness
    expected_xor = hash_transformed(src, TABLE_XOR)
    expected_or = hash_transformed(src, TABLE_OR)
    got_copy = hash_file(out)
    got_inplace = hash_file(work)

    print()
    print(f"  copy    output {'OK' if got_copy == expected_xor else 'WRONG'} ({got_copy[:16]})")
    print(f"  inplace output {'OK' if got_inplace == expected_or else 'WRONG'} ({got_inplace[:16]})")

    if copy_payload > 0:
        print(f"\n  inplace / copy = {inplace_payload / copy_payload:.2f}x payload throughput")

    return 0 if (got_copy == expected_xor and got_inplace == expected_or) else 1


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
