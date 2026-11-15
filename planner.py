#!/usr/bin/env python3
"""Plan hardware access instead of hard-coding it.

Measures the device it is about to use, decides how to move the bytes, states
the reasoning, then executes and compares the result against its own
prediction. A plan that cannot be checked is just an opinion.

    py planner.py --src big.bin --dst out.bin
    py planner.py --size-mib 256 --dir C:\\Users\\kevin\\io_bench_work --direct

Decisions it makes:
    compress or not        from a sample of the actual data
    read/write shape       from measured read and write ceilings
    chunk size             larger on devices with unstable timing
    queue depth            recommendation only (depth-1 execution in v1)

Everything is archived under results\\run_NNN\\ with the plan, the numbers and
a verdict, so a wrong plan leaves evidence rather than a shrug.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import statistics
import struct
import sys
import time
import zlib

CHUNK_SMALL = 1 << 20
CHUNK_LARGE = 4 << 20
SECTOR = 4096
SAMPLE = 4 << 20
MAGIC = 0x5A
TABLE = bytes(b ^ MAGIC for b in range(256))


# ---------------------------------------------------------------------------
# raw I/O: cache-bypassed on Windows, buffered elsewhere
# ---------------------------------------------------------------------------
class Direct:
    """Sector-aligned, FILE_FLAG_NO_BUFFERING handles."""

    def __init__(self):
        if platform.system() != "Windows":
            raise RuntimeError("direct I/O is Windows only")
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                    wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                    wintypes.HANDLE]
        k32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                                 ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        k32.WriteFile.argtypes = list(k32.ReadFile.argtypes)
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.k32 = k32
        self.wintypes = wintypes
        self.INVALID = ctypes.c_void_p(-1).value
        self.raw = ctypes.create_string_buffer(CHUNK_LARGE + SECTOR * 2)
        self.ptr = (ctypes.addressof(self.raw) + SECTOR - 1) & ~(SECTOR - 1)
        self.got = wintypes.DWORD(0)
        self.put = wintypes.DWORD(0)

    def _open(self, path, write):
        GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
        OPEN_EXISTING, CREATE_ALWAYS = 3, 2
        FILE_ATTRIBUTE_NORMAL = 0x80
        NO_BUFFERING = 0x20000000
        access = GENERIC_WRITE if write else GENERIC_READ
        disposition = CREATE_ALWAYS if write else OPEN_EXISTING
        handle = self.k32.CreateFileW(path, access, 0 if write else 1, None, disposition,
                                      FILE_ATTRIBUTE_NORMAL | NO_BUFFERING, None)
        if handle == self.INVALID:
            raise OSError(self.ctypes.get_last_error(), f"CreateFileW({path})")
        return handle

    def write_file(self, path, size, fill=b"\x00"):
        handle = self._open(path, True)
        try:
            block = (fill * (CHUNK_LARGE // len(fill) + 1))[:CHUNK_LARGE]
            remaining = size
            while remaining > 0:
                n = min(CHUNK_LARGE, remaining)
                n = (n // SECTOR) * SECTOR
                if n == 0:
                    break
                self.ctypes.memmove(self.ptr, block[:n], n)
                done = 0
                while done < n:
                    if not self.k32.WriteFile(handle, self.ctypes.c_void_p(self.ptr + done),
                                              n - done, self.ctypes.byref(self.put), None):
                        raise OSError(self.ctypes.get_last_error(), "WriteFile")
                    done += self.put.value
                remaining -= n
        finally:
            self.k32.CloseHandle(handle)

    def read_file(self, path, size):
        handle = self._open(path, False)
        total = 0
        try:
            remaining = size
            while remaining > 0:
                n = (min(CHUNK_LARGE, remaining) // SECTOR) * SECTOR
                if n == 0:
                    break
                if not self.k32.ReadFile(handle, self.ctypes.c_void_p(self.ptr), n,
                                         self.ctypes.byref(self.got), None):
                    raise OSError(self.ctypes.get_last_error(), "ReadFile")
                if self.got.value == 0:
                    break
                total += self.got.value
                remaining -= self.got.value
        finally:
            self.k32.CloseHandle(handle)
        return total

    def copy(self, src, dst, size, table=TABLE):
        hin = self._open(src, False)
        hout = self._open(dst, True)
        try:
            remaining = size
            while remaining > 0:
                n = (min(CHUNK_LARGE, remaining) // SECTOR) * SECTOR
                if n == 0:
                    break
                if not self.k32.ReadFile(hin, self.ctypes.c_void_p(self.ptr), n,
                                         self.ctypes.byref(self.got), None):
                    raise OSError(self.ctypes.get_last_error(), "ReadFile")
                got = self.got.value
                if got == 0:
                    break
                chunk = self.ctypes.string_at(self.ptr, got).translate(table)
                self.ctypes.memmove(self.ptr, chunk, len(chunk))
                done = 0
                while done < len(chunk):
                    if not self.k32.WriteFile(hout, self.ctypes.c_void_p(self.ptr + done),
                                              len(chunk) - done,
                                              self.ctypes.byref(self.put), None):
                        raise OSError(self.ctypes.get_last_error(), "WriteFile")
                    done += self.put.value
                remaining -= got
        finally:
            self.k32.CloseHandle(hin)
            self.k32.CloseHandle(hout)


def queued_copy(src, dst, size, depth):
    """Unbuffered copy with `depth` requests in flight (Windows, IOCP).

    Each slot owns its own aligned buffer and OVERLAPPED, so offsets never
    collide. Returns the number of bytes written. Any failure raises, and the
    caller falls back to the sequential path - an untested engine must never
    be able to break a run.
    """
    if platform.system() != "Windows":
        raise RuntimeError("queued copy needs Windows")

    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
    OPEN_EXISTING, CREATE_ALWAYS = 3, 2
    FILE_ATTRIBUTE_NORMAL = 0x80
    NO_BUFFERING = 0x20000000
    OVERLAPPED_FLAG = 0x40000000
    ERROR_IO_PENDING = 997
    INVALID = ctypes.c_void_p(-1).value

    class OVERLAPPED(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE)]

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

    flags = FILE_ATTRIBUTE_NORMAL | NO_BUFFERING | OVERLAPPED_FLAG
    fin = k32.CreateFileW(src, GENERIC_READ, 1, None, OPEN_EXISTING, flags, None)
    if fin == INVALID:
        raise OSError(ctypes.get_last_error(), "CreateFileW(src)")
    fout = k32.CreateFileW(dst, GENERIC_WRITE, 0, None, CREATE_ALWAYS, flags, None)
    if fout == INVALID:
        k32.CloseHandle(fin)
        raise OSError(ctypes.get_last_error(), "CreateFileW(dst)")

    iocp = k32.CreateIoCompletionPort(INVALID, None, 0, 0)
    k32.CreateIoCompletionPort(fin, iocp, 1, 0)
    k32.CreateIoCompletionPort(fout, iocp, 2, 0)

    slots = []
    for _ in range(depth):
        raw = ctypes.create_string_buffer(CHUNK_SMALL + SECTOR)
        ptr = (ctypes.addressof(raw) + SECTOR - 1) & ~(SECTOR - 1)
        slots.append({"raw": raw, "ptr": ptr, "ov": OVERLAPPED(),
                      "offset": 0, "length": 0, "state": "idle"})
    by_ov = {ctypes.addressof(slot["ov"]): slot for slot in slots}

    def issue_read(slot, offset):
        length = (min(CHUNK_SMALL, size - offset) // SECTOR) * SECTOR
        if length == 0:
            return False
        slot["offset"], slot["length"], slot["state"] = offset, length, "read"
        slot["ov"].Offset = offset & 0xFFFFFFFF
        slot["ov"].OffsetHigh = (offset >> 32) & 0xFFFFFFFF
        if not k32.ReadFile(fin, ctypes.c_void_p(slot["ptr"]), length, None,
                            ctypes.byref(slot["ov"])):
            err = ctypes.get_last_error()
            if err != ERROR_IO_PENDING:
                raise OSError(err, "ReadFile")
        return True

    def issue_write(slot):
        slot["state"] = "write"
        if not k32.WriteFile(fout, ctypes.c_void_p(slot["ptr"]), slot["length"], None,
                             ctypes.byref(slot["ov"])):
            err = ctypes.get_last_error()
            if err != ERROR_IO_PENDING:
                raise OSError(err, "WriteFile")

    next_offset = 0
    pending = 0
    for slot in slots:
        if next_offset >= size or not issue_read(slot, next_offset):
            break
        next_offset += slot["length"]
        pending += 1

    written = 0
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
                issue_write(slot)
            else:
                written += slot["length"]
                if next_offset < size and issue_read(slot, next_offset):
                    next_offset += slot["length"]
                else:
                    pending -= 1
    finally:
        k32.CloseHandle(fout)
        k32.CloseHandle(fin)
        k32.CloseHandle(iocp)
    return written


def buffered_write(path, size, fill=b"\x00"):
    block = (fill * (CHUNK_SMALL // len(fill) + 1))[:CHUNK_SMALL]
    with open(path, "wb", buffering=0) as handle:
        remaining = size
        while remaining > 0:
            n = min(CHUNK_SMALL, remaining)
            handle.write(block[:n])
            remaining -= n


def buffered_read(path, size):
    total = 0
    with open(path, "rb", buffering=0) as handle:
        remaining = size
        while remaining > 0:
            chunk = handle.read(min(CHUNK_SMALL, remaining))
            if not chunk:
                break
            total += len(chunk)
            remaining -= len(chunk)
    return total


def buffered_copy(src, dst, size, table=TABLE):
    with open(src, "rb", buffering=0) as fin, open(dst, "wb", buffering=0) as fout:
        remaining = size
        while remaining > 0:
            chunk = fin.read(min(CHUNK_SMALL, remaining))
            if not chunk:
                break
            fout.write(chunk.translate(table))
            remaining -= len(chunk)


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------
def timed(func, runs=2):
    times = []
    for index in range(runs + 1):
        start = time.perf_counter()
        func()
        elapsed = time.perf_counter() - start
        if index:
            times.append(elapsed)
    median = statistics.median(times)
    spread = (max(times) - min(times)) / median if median else 0.0
    return median, spread


def probe(directory, size, use_direct):
    """Measure this device: read ceiling, write ceiling, stability."""
    probe_dir = os.path.join(directory, "_probe")
    os.makedirs(probe_dir, exist_ok=True)
    blob = os.path.join(probe_dir, "probe.bin")

    engine = None
    if use_direct:
        try:
            engine = Direct()
        except Exception as exc:            # noqa: BLE001 - report, then continue
            print(f"  direct unavailable ({exc}); falling back to buffered")
    write = engine.write_file if engine else buffered_write
    read = engine.read_file if engine else buffered_read

    write_median, write_spread = timed(lambda: write(blob, size))
    read_median, read_spread = timed(lambda: read(blob, size))

    mib = size / (1024 * 1024)
    return {
        "write_mib_s": round(mib / write_median, 1),
        "read_mib_s": round(mib / read_median, 1),
        "write_spread": round(write_spread, 2),
        "read_spread": round(read_spread, 2),
        "transform_mib_s": transform_speed(),
        "engine": "direct" if engine else "buffered",
    }


def transform_speed(sample_bytes=1 << 20):
    """How fast the copy loop's per-byte work runs, away from the device.

    On slow media this is irrelevant; on anything doing gigabytes per second
    the translate step becomes the binding cost, and ignoring it makes the
    prediction wildly optimistic.
    """
    data = os.urandom(sample_bytes)
    start = time.perf_counter()
    data.translate(TABLE)
    elapsed = time.perf_counter() - start
    return round((len(data) / (1024 * 1024)) / elapsed, 1) if elapsed else 0.0


def compressibility(path, level=1, sample=SAMPLE):
    with open(path, "rb") as handle:
        data = handle.read(sample)
    if not data:
        return 1.0, 0.0
    start = time.perf_counter()
    packed = zlib.compress(data, level)
    elapsed = time.perf_counter() - start
    speed = (len(data) / (1024 * 1024)) / elapsed if elapsed else 0.0
    return len(packed) / len(data), round(speed, 1)


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------
def make_plan(device, ratio, compress_mib_s, size, min_saving):
    notes = []
    read_s = device["read_mib_s"] or 1.0
    write_s = device["write_mib_s"] or 1.0

    payload_mib = size / (1024 * 1024)
    transform_s = device.get("transform_mib_s") or 1.0e9
    # read, transform and write happen in series, so the times add up
    copy_seconds = (payload_mib / read_s + payload_mib / transform_s
                    + payload_mib / write_s)
    predicted_plain = payload_mib / copy_seconds

    if device["transform_mib_s"] < min(read_s, write_s) * 2:
        notes.append(f"transform {device['transform_mib_s']} MiB/s is close to the "
                     f"device speed: the copy is partly CPU-bound")

    if write_s < read_s * 0.8:
        notes.append(f"write {write_s} MiB/s is below read {read_s} MiB/s: "
                     f"a copy is write-bound")

    unstable = max(device["write_spread"], device["read_spread"]) > 0.25
    if unstable:
        notes.append(f"timing spread {max(device['write_spread'], device['read_spread']):.0%}: "
                     f"seek-sensitive, keep streams sequential")
        chunk = CHUNK_LARGE
        depth = 1
    else:
        notes.append("timing is stable: this looks like solid-state media")
        chunk = CHUNK_SMALL
        depth = 16

    packed_mib = payload_mib * ratio
    pack_seconds = payload_mib / read_s + payload_mib / max(compress_mib_s, 1.0) \
        + packed_mib / write_s
    predicted_packed = payload_mib / pack_seconds

    use_packed = ratio < (1.0 - min_saving)
    if use_packed:
        notes.append(f"sample compresses {ratio:.3f}: pack it "
                     f"({payload_mib:.0f} MiB -> {packed_mib:.0f} MiB written)")
    else:
        notes.append(f"sample compresses {ratio:.3f}: packing would cost CPU "
                     f"for no saving")

    return {
        "payload_mib": round(payload_mib, 1),
        "compress": use_packed,
        "chunk_bytes": chunk,
        "queue_depth": depth,
        "predicted_mib_s": round(predicted_packed if use_packed else predicted_plain, 1),
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------
def frame(chunk, level):
    packed = zlib.compress(chunk, level)
    record = struct.pack("<II", len(chunk), len(packed)) + packed
    return record + bytes((-len(record)) % 4096)


def run_plain(src, dst, size, use_direct, depth=1):
    """Execute the plain path, honouring the planned depth where possible.

    The queued engine is newer than the sequential one, so it is attempted
    first and abandoned on any error: a plan should never fail because the
    faster path is not available on this machine.
    """
    if use_direct and depth > 1:
        try:
            queued_copy(src, dst, size, depth)
            return "queued"
        except Exception as exc:            # noqa: BLE001 - fall back, don't fail
            print(f"  queued path unavailable ({type(exc).__name__}: {exc}); "
                  f"using sequential")
    if use_direct:
        try:
            Direct().copy(src, dst, size)
            return "direct"
        except Exception as exc:            # noqa: BLE001 - degrade, don't fail
            print(f"  direct unavailable ({type(exc).__name__}: {exc}); "
                  f"using buffered")
    buffered_copy(src, dst, size)
    return "buffered"


def run_packed(src, dst, size, use_direct, level):
    written = 0
    with open(src, "rb", buffering=0) as fin:
        sink = open(dst, "wb", buffering=0)
        try:
            remaining = size
            while remaining > 0:
                chunk = fin.read(min(CHUNK_LARGE, remaining))
                if not chunk:
                    break
                # transform first, so packed and plain produce the same bytes
                sink.write(frame(chunk.translate(TABLE), level))
                written += 1
                remaining -= len(chunk)
        finally:
            sink.close()


# ---------------------------------------------------------------------------
def expected_hash(src):
    """Hash of the transformed payload - what any correct output must match."""
    digest = hashlib.blake2b()
    with open(src, "rb") as handle:
        while True:
            chunk = handle.read(CHUNK_SMALL)
            if not chunk:
                break
            digest.update(chunk.translate(TABLE))
    return digest.hexdigest()


def plain_hash(path):
    digest = hashlib.blake2b()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(CHUNK_SMALL)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def unpack(path):
    """Reverse the framed archive; returns (hash of payload, bytes recovered)."""
    digest = hashlib.blake2b()
    total = 0
    with open(path, "rb") as handle:
        while True:
            header = handle.read(8)
            if len(header) < 8:
                break
            original, packed = struct.unpack("<II", header)
            digest.update(zlib.decompress(handle.read(packed)))
            total += original
            padding = (-(8 + packed)) % 4096
            if padding:
                handle.seek(padding, os.SEEK_CUR)
    return digest.hexdigest(), total


def archive(plan, device, result, verdict, workdir):
    results = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
    os.makedirs(results, exist_ok=True)
    numbers = [0]
    for entry in os.listdir(results):
        if entry.startswith("run_"):
            try:
                numbers.append(int(entry.split("_")[1]))
            except (IndexError, ValueError):
                pass
    run = max(numbers) + 1
    target = os.path.join(results, f"run_{run:03d}_{time.strftime('%Y-%m-%d_%H%M%S')}")
    os.makedirs(target, exist_ok=True)
    payload = {"run": run, "device": device, "plan": plan, "result": result,
               "verdict": verdict, "workdir": workdir,
               "platform": platform.platform()}
    with open(os.path.join(target, "plan.json"), "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    return target


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="probe, plan, execute, verify")
    parser.add_argument("--src", default=None, help="payload to move")
    parser.add_argument("--dst", default=None, help="destination")
    parser.add_argument("--size-mib", type=int, default=256)
    parser.add_argument("--dir", default=None, help="scratch directory for the probe")
    parser.add_argument("--direct", action="store_true")
    parser.add_argument("--level", type=int, default=1)
    parser.add_argument("--min-saving", type=float, default=0.1)
    parser.add_argument("--probe-mib", type=int, default=32)
    args = parser.parse_args(argv)

    workdir = args.dir or os.path.dirname(os.path.abspath(__file__))
    os.makedirs(workdir, exist_ok=True)

    src = args.src
    if not src:
        src = os.path.join(workdir, "plan_input.bin")
        if not os.path.exists(src) or os.path.getsize(src) != args.size_mib * 1024 * 1024:
            print(f"generating {args.size_mib} MiB workload (highly compressible)")
            text = (b"anvil telemetry record: device=nvme0 state=ok queue=32\n" * 1024)
            with open(src, "wb") as handle:
                remaining = args.size_mib * 1024 * 1024
                while remaining > 0:
                    take = min(len(text), remaining)
                    handle.write(text[:take])
                    remaining -= take

    size = os.path.getsize(src)
    dst = args.dst or (src + ".out")

    print(f"planner  python {platform.python_version()}  {platform.platform()}")
    print(f"payload  {src}  ({size / (1024 * 1024):.1f} MiB)\n")

    print("probing device...")
    device = probe(workdir, args.probe_mib * 1024 * 1024, args.direct)
    print(f"  read      {device['read_mib_s']:>8.1f} MiB/s  (spread {device['read_spread']:.0%})")
    print(f"  write     {device['write_mib_s']:>8.1f} MiB/s  (spread {device['write_spread']:.0%})")
    print(f"  transform {device['transform_mib_s']:>8.1f} MiB/s  (per-byte CPU work)")
    print(f"  engine {device['engine']}\n")

    ratio, compress_mib_s = compressibility(src, args.level)
    print(f"probing data...  sample ratio {ratio:.3f}, zlib {compress_mib_s:.0f} MiB/s\n")

    plan = make_plan(device, ratio, compress_mib_s, size, args.min_saving)
    print("plan:")
    for note in plan["notes"]:
        print(f"  - {note}")
    print(f"  -> {'packed' if plan['compress'] else 'plain'}, "
          f"chunk {plan['chunk_bytes'] >> 10} KiB, depth {plan['queue_depth']} "
          f"(recommended)")
    print(f"  -> predicted {plan['predicted_mib_s']} MiB/s\n")

    print("executing...")
    start = time.perf_counter()
    if plan["compress"]:
        run_packed(src, dst, size, args.direct, args.level)
        executed_with = "packed"
        written = os.path.getsize(dst)
    else:
        executed_with = run_plain(src, dst, size, args.direct, plan["queue_depth"])
        written = os.path.getsize(dst)
    elapsed = time.perf_counter() - start
    print(f"  engine used: {executed_with}")

    actual = (size / (1024 * 1024)) / elapsed
    error = abs(actual - plan["predicted_mib_s"]) / max(plan["predicted_mib_s"], 1.0)

    # correctness first: a fast wrong answer is not a plan that worked
    expected = expected_hash(src)
    recovered = 0
    try:
        if plan["compress"]:
            got, recovered = unpack(dst)
        else:
            got, recovered = plain_hash(dst), written
        verified = got == expected
    except Exception as exc:            # noqa: BLE001 - corrupt archive is a result
        print(f"  verify cannot read the output: {type(exc).__name__}: {exc}")
        verified = False

    print(f"  {elapsed:.3f} s   payload {actual:.1f} MiB/s   "
          f"wrote {written / (1024 * 1024):.1f} MiB")
    print(f"  prediction {plan['predicted_mib_s']} MiB/s   "
          f"error {error:.0%}")
    print(f"  verify {'OK' if verified else 'WRONG'} "
          f"({recovered} of {size} byte(s) recovered)")

    # Two separate claims: did the data arrive intact, and was the model right?
    # A wrong prediction is a fact about the model, not a failed transfer.
    if not verified:
        verdict = "FAIL (output does not match the input)"
    else:
        verdict = "PASS (verified)"
    accuracy = ("within the 25% target" if error <= 0.25
                else "above the 25% target - the model needs work")
    print(f"  verdict: {verdict}")
    print(f"  model accuracy: {accuracy}")

    result = {"elapsed_s": round(elapsed, 3), "payload_mib_s": round(actual, 1),
              "written_mib": round(written / (1024 * 1024), 1),
              "prediction_error": round(error, 3), "verified": verified,
              "executed_with": executed_with}
    target = archive(plan, device, result, verdict, workdir)
    print(f"\narchived: {target}")

    return 0 if verified else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
