# io_bench

Measure how fast an agent can move bytes through a machine — and keep every
result reproducible and auditable.

The workload is deliberately boring: read a file, transform every byte, write
it back out. What changes is the **data path**.

| Path | What it does | What it isolates |
|---|---|---|
| `buffered` | Python `read()`/`write()` in 1 MiB chunks | the normal application path |
| `mmap` | both files mapped, transformed in place | page-cache copy behaviour |
| `direct` | `FILE_FLAG_NO_BUFFERING` + sector-aligned buffers, one operation at a time (Windows) | the device with **zero queue depth** |
| `iocp@N` | the same unbuffered path with **N operations in flight** via I/O completion ports (Windows) | whether the limit is the device or the serial request |
| `phased` | read a whole batch, then write it | head thrash: read/write alternation vs long sequential runs |

All paths must produce **byte-identical output** — the harness hashes the
result and warns if they differ, because comparing timings on different work
is meaningless.

## The finding, up front

On a machine with an NVMe that reads at 2.2 GB/s and writes at 265 MB/s:

- **queue depth does not help** (56 -> 75 MiB/s from depth 1 to 64)
- **rewriting in place is worse than copying** (0.64x SSD, 0.75x HDD)
- **compressing compressible data is 3.6x faster** (256 MiB written as 4 MiB)
- **compressing incompressible data costs 5x** for nothing

So the lever is not how writes are issued, it is how many bytes are written -
and that depends on the data, which an agent can sample before deciding.
`choose.py` does exactly that and verifies the result on the way out.

## Quick start

```
run_bench.bat
```

or directly:

```
py bench.py                        # 256 MiB, 3 measured runs each
py bench.py --size-mib 1024 --runs 5
py bench.py --methods buffered,mmap
py bench.py --list
```

Nothing outside the standard library. `direct` needs Windows; other platforms
skip it and say so.

## Methodology

- one warm-up pass per method, discarded
- N measured runs, **median** reported (an outlier can't flatter a method)
- MiB/s computed from the median wall time
- output hash recorded per method; mismatches are reported loudly
- every run archived under `results/run_NNN_<timestamp>/` with
  `meta.json` (host, Python version, sizes, timings, script SHA-256) and
  `bench.log` (the full transcript)
- `results/runs.json` is the append-only catalogue

## Queue depth: the actual experiment

`direct` issues one read, waits, then one write, and waits again. On a modern
device that is a latency measurement, not a bandwidth one: the drive sits idle
between operations.

`iocp@N` keeps N reads and writes overlapping. The expected shape is:

```
iocp@1    ~= direct        (sanity check - both are depth 1)
iocp@4    several times faster
iocp@16   faster still
iocp@64   flat: the device is saturated
```

The depth at which it flattens is the useful number. Below it, an agent is
limited by its own request pattern, not by the hardware - and that is fixable
in software.

```
run_bench.bat                                         # sweep 1,4,16,64
py bench.py --methods buffered,mmap,iocp --size-mib 512
py bench.py --depths 1,2,4,8,16,32 --runs 5
```

The full sweep takes about a minute at 256 MiB, because depth 1 must finish
its 19 seconds of waiting before the interesting numbers appear.

## The policy this produces: `choose.py`

Four experiments, four answers. The one that matters is not how to issue
writes but how many bytes to write - and that depends on the data.

```
py choose.py --src big.bin --dst big.out --direct
```

It samples the first 4 MiB, compresses it, and keeps the compressed form only
if it saves at least `--min-saving` (default 10%). With 256 MiB of log-like
text and `--direct`:

```
probe   4 MiB -> compressed ratio 0.013 (99% smaller)
policy  threshold 0.90 -> COMPRESS
median 0.102 s   payload  2519.44 MiB/s
wrote       4.0 MiB  (0.02x the input)
verify OK (268435456 bytes recovered)
```

Same command against incompressible data:

```
probe   4 MiB -> compressed ratio 1.055 (-5% smaller)
policy  threshold 0.90 -> WRITE PLAIN
median 0.285 s   payload   898.35 MiB/s
wrote     256.0 MiB  (1.00x the input)
verify OK (268435456 bytes recovered)
```

An agent can make that call every time. A human rarely bothers.

## Findings so far (256 MiB, Windows 11, spinning disk on D:)

| Path | MiB/s |
|---|---|
| `buffered` | 1247 |
| `mmap` | 482 |
| `direct` | 56 |
| `iocp@1` | 61 |
| `iocp@4` | 72 |
| `iocp@16` | 57 |
| `iocp@64` | 75 |

### Lever-by-lever results

| Lever | Measured | Verdict |
|---|---|---|
| Queue depth 1 -> 64 | 56 -> 75 MiB/s | no effect (device already saturated) |
| In-place vs copy (SSD) | 184 vs 287 MiB/s payload | **worse** - 0.64x |
| In-place vs copy (HDD) | 41 vs 55 MiB/s payload | **worse** - 0.75x |
| Compress compressible text | 626 -> 2277 MiB/s, 64x fewer bytes | **3.6x faster** |
| Compress incompressible data | 648 -> 112 MiB/s, 0.9x bytes | **worse** - 0.17x |

Three conclusions, all reproducible:

1. **Queue depth does not help.** 56 → 75 MiB/s from depth 1 to 64 is noise.
   The device is already saturated at depth 1. Adding in-flight requests to a
   single spindle only creates seek contention - `@16` came out *slower* than
   `@4`.
2. **The conventional cache path is ~20x faster than the honest one.** That gap
   is RAM vs rotating media, not software.
3. **Variance is high** (3.17-4.85 s for identical work), which is itself the
   signature of mechanical seeking.

On spinning media, "more parallelism" is the wrong instinct. The levers are
longer sequential runs and fewer concurrent streams:

```
py bench.py --methods buffered,phased --size-mib 256 --chunk-kib 1024
py bench.py --methods buffered,phased --size-mib 256 --chunk-kib 4096
py bench.py --methods buffered,phased --size-mib 256 --batch-mib 64
```

And to separate "this disk" from "this software", run the same sweep on an SSD:

```
py bench.py --workdir C:\\io_bench_work --size-mib 256 \
            --methods buffered,mmap,direct,iocp --depths 1,8,64
```

If the SSD shows queue-depth scaling and the HDD does not, that difference *is*
 the finding: the same agent code needs different I/O shapes depending on the
medium, and no single queue depth is optimal for both.

## How to read the numbers honestly

- **`buffered` and `mmap` are cache measurements as much as disk ones.** On a
  machine with free RAM, the second run reads from page cache. That is a real
  result for an agent re-reading its own data — and a misleading one if you
  meant to measure the device.
- **`direct` bypasses the cache**, so it shows device behaviour. It will often
  look *slower* on cached data and faster on cold data. Both are true.
- **Single-threaded, sequential, one file.** Queue-depth and parallelism are
  where modern I/O wins live; they are not measured here yet.
- **The transform is `bytes.translate`** — implemented in C — so the Python
  interpreter is not the bottleneck.
- Variance is real: run on a quiet machine, and prefer 5 runs over 3.

## What this is for

Agents move data constantly. Before claiming a faster path, there has to be a
baseline anyone can reproduce. This is the baseline: one command, one hash,
one honest number.

## Limits

- no parallelism, no queue depth, no O_DIRECT equivalent on Linux yet
- no cold-cache protocol (dropping caches needs admin rights)
- no power/thermal control, so laptop numbers drift
- toy workload: real applications interleave reads, writes and compute

## Where it writes

`io_bench` generates its own workload inside `--workdir` (256 MiB by default). `inplace.py`
rewrites that file in place rather than copying it first, so a scratch directory is the
intended home for a run.

[`SECURITY.md`](SECURITY.md) covers the rest, including how to report a problem.

## Repository layout

```
bench.py       compare data paths (buffered, mmap, direct, iocp@N, phased)
inplace.py     copy vs in-place rewrite, buffered and cache-bypassed
compress.py    writes with and without compression, framed and verified
choose.py      probe the data, pick a shape, verify the result
reproduce.bat  runs every experiment in order
results/       numbered, timestamped, hashed run archives
```

## Licence

MIT — see `LICENSE`.


## Project documents

| Document | What it covers |
|---|---|
| [CREDITS.md](CREDITS.md) | who contributed what, and how the work was produced |
| [CHANGELOG.md](CHANGELOG.md) | what changed, per version |
| [CONTRIBUTING.md](CONTRIBUTING.md) | how to send a change, and the house rules |
| [SECURITY.md](SECURITY.md) | known exposures, and how to report a problem |
