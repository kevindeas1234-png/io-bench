# Security

No security review has been performed on this project. It is a hobby-scale codebase with
no security response process and no guarantee of a fix.

## Reporting

Open an issue titled `[security] ...` with a description but without a working exploit.
Expect no particular response time.

## Known exposures, by design

| Writes to `--workdir` | The benchmark generates its own 256 MiB workload there, and `inplace.py` rewrites that file rather than copying it first. Point it at scratch space. |

## General

The documentation states plainly what each tool does at its edges. Where a risk is inherent
to the design rather than an oversight, it is described there rather than left to be
discovered.