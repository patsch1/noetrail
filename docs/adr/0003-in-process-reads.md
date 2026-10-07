# ADR 0003: In-process reads in the Noetrail MCP server

- Status: accepted
- Date: 2026-08-03
- Roadmap phase: 11

## Follow-up

The review follow-up adds 30-second cooperative deadlines to scan and scoring
loops. An expiry returns `read_timeout` and releases the lock. Blocking I/O
still needs `--subprocess-reads` for a hard timeout; the original rationale
and measurements below describe the decision when introduced.

## Context

The Noetrail MCP server started as a thin bridge: every tool call was
translated into a `noetrail.cli` argument vector and run as a separate CLI
process. That gave two properties for free — a crashed or wedged operation
could not take the server with it, and every call inherited a 30-second hard
timeout — and it guaranteed that the CLI stayed the single implementation of
vault rules.

It also made every call pay for a fresh interpreter. The Phase 10 measurement
(reproducible with `tools/measure_search.py`, recorded in
[Limits and scaling](../limits.md)) separated the two costs:

| Entries | Interpreter startup | In-process scan | Full CLI subprocess |
| ---: | ---: | ---: | ---: |
| 100 | 15 ms | 2 ms | 50 ms |
| 1,000 | 17 ms | 25 ms | 87 ms |
| 5,000 | 15 ms | 134 ms | 261 ms |

The linear scan costs about 27 µs per entry. Interpreter startup, module
import, and argument parsing cost a fixed ~47 ms and dominate every query below
roughly 2,000 entries. An agent turn is not one call: a representative turn is
an inventory, three searches, and two entry reads, so the fixed cost is paid
six times.

The obvious response — add a search index — would attack the part of the cost
that was not the problem, and would add a permanent consistency obligation.
Removing the per-call process was the larger and cheaper win. The Phase 10
package boundary (`src/noetrail/`, one importable package) is what made calling
the CLI in-process possible at all.

## Decision

Answer the frequent bounded read operations inside the MCP server process, and
keep everything else in a separate CLI process.

In-process: `inventory`, `search`, `get_entry`, `review_queue`, `relations`,
and `list_trash`. These are the calls an agent makes several times per turn.
`MCPServer._run_read()` dispatches them to `run_command()` with stdout and
stderr redirected, under the same shared vault lock the subprocess path takes.

In a separate process: every mutation, and `validate`. These are rarer, they
are the ones where a partial write matters, and they keep both the crash
isolation and the 30-second `TOOL_TIMEOUT_SECONDS` bound.

Both paths execute the same code with the same argument vector; only the
process boundary differs. `noetrail-mcp --subprocess-reads` puts reads back on
the old path without a release, and an explicit CLI-script override continues
to force every call through a process, because that override exists to
substitute a different implementation.

## Measured outcome

Same measurement script, one representative six-call turn:

| Entries | Separate CLI process per read | Read in the server process |
| ---: | ---: | ---: |
| 100 | 316 ms | 32 ms |
| 1,000 | 494 ms | 199 ms |
| 5,000 | 1,350 ms | 1,049 ms |

The saving is the expected constant: about 300 ms per turn, independent of
vault size. Below roughly 1,000 entries that is nearly the whole turn latency.

## Consequences

Benefits:

- an agent turn on a small vault costs a tenth of what it did;
- no index, no second copy of the data, no rebuild obligation;
- the CLI remains the single implementation of vault rules, so MCP and CLI
  cannot drift apart in what they validate.

Costs, and one of them is not yet paid off:

- **In-process reads have no timeout.** The subprocess path bounded every call
  at 30 seconds. A read that runs in the server process runs to completion, so
  on a very large vault a single scan occupies the server until it finishes and
  only the client's own timeout ends the call. There is no cooperative
  cancellation inside the scan today. `--subprocess-reads` is the documented
  escape hatch, and a real bound is open work listed in
  [ROADMAP.md](../../ROADMAP.md).
- An unhandled exception in a read path now surfaces in the server process
  instead of as a non-zero exit code from a child. The read path catches
  `SystemExit` and converts it, but a genuine crash is no longer contained.
- Memory used by a read is held by the long-lived server process rather than
  released when a child exits.
- The two paths must stay behaviourally identical. That is a test obligation,
  covered by `tests/test_in_process.py`.

## Alternatives considered

**Keep a subprocess per call.** Rejected: it charges ~300 ms per agent turn for
isolation that the read operations, which take no locks they do not release and
write nothing, do not need.

**Build a full-text index instead.** Rejected for this problem: the index
attacks the scan, which was not the dominant cost below 2,000 entries, and it
costs a permanent consistency obligation. Recorded separately in
[ADR 0004](0004-no-search-index.md).

**A persistent CLI worker process.** Rejected: it keeps the isolation and the
timeout but adds process supervision, a wire protocol between the server and
its worker, and a second lifecycle to get wrong — for the same saving that
importing the module achieves with no moving parts.

**Run reads in a thread with a timeout.** Rejected for now: Python cannot
interrupt a running scan from another thread, so the timeout would report a
failure while the work continued in the background. A real bound has to be
cooperative and belongs inside the scan loop, which is the open work above.
