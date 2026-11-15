# Contributing

A small, single-maintainer project. Issues are welcome; pull requests are considered but
not promised. There is no support commitment.

## Before sending anything

Run the benchmark and paste its output in the thread:

```
py bench.py --size-mib 64 --runs 1
```

Include the machine (CPU, storage device) and the "all methods produced identical output"
line, because a timing without its data path means nothing.

## House rules

- **Disclose AI assistance.** If a tool wrote a substantial part of your contribution, say
  so, or use an `Assisted-by:` trailer. `Co-authored-by` is reserved for people.
- **Same work or no comparison.** A new data path must produce byte-identical output to the
  existing ones, or it is measuring something else.
- **Report medians and runs**, not the best case.
- **One change per commit**, with a message that explains why.