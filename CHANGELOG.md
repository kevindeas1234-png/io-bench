# Changelog

All notable changes to io_bench. Format follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added
- `CREDITS.md`, `CONTRIBUTING.md`, `SECURITY.md`, `.gitattributes`.

### Changed
- Personal data removed from the documentation.

## [0.1.0] — 2026-11-15

First tagged release.

### Added
- Five data paths: `buffered`, `mmap`, `direct` (FILE_FLAG_NO_BUFFERING with aligned
  buffers), `iocp@N` (unbuffered with N operations in flight) and `phased` (read a batch,
  then write it).
- Byte-identical output enforcement, so timings are only ever compared on identical work.
- `planner.py`, which samples the data before deciding whether writing less is worth it.
- Findings in the README: queue depth barely helps; writing fewer bytes matters far more.