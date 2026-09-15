# Supervisor exit evidence

The private supervisor keeps safe evidence of its own decisions. This does not
identify the historical shutdown cause, prove gateway readiness, or change the
three-attempt policy. The launchd specification and gateway command are unchanged.

`state/private-supervisor-events.json` holds the latest invocation. At the first
successful journal write of a new invocation, the former file is atomically moved
to `private-supervisor-events.previous.json`, preserving its terminal evidence.
No previous content is loaded into memory. A failed rotation prevents overwriting
that evidence. Both managed files are private (0600) and capped at 64 KiB. The ring
holds at most 64 events; `dropped_events` counts evictions, and `write_failures`
counts failed journal attempts visible at the next successful write. JSON is
written with the existing `atomic_json` primitive and private-directory checks.

Events are `SUPERVISOR_START`, `ATTEMPT`, `CHILD_STARTED`,
`CHILD_WAIT_RETURNED`, `STOP_REQUESTED`, `SUPERVISOR_EXIT`,
`SUPERVISOR_ERROR`, and `TERMINAL_STATE_WRITE_FAILED`. Each contains UTC,
monotonic seconds, phase, attempt, owned supervisor/child PIDs, observed child
exit code or null, intended supervisor return code or null, and the stop flag.
Errors contain only `SUPERVISOR_ERROR` and a closed exception category:
`ConfigError`, `ProcessLookupError`, `PermissionError`, `OSError`, `ValueError`,
or `Exception`. Messages, class names from subclasses, tracebacks, configuration,
environment, command lines, child stderr and speech are excluded.

Phases distinguish setup, initial state write, child creation, wait, final state
write, retry delay, signal polling/termination, and failed terminal-state writing.
`CHILD_STARTED` proves only that `Popen` returned. `child_exit_code` becomes known
only after `wait()` returns, including negative signal return codes. An error
during wait or termination does not prove child death. `supervisor_exit_code`
describes the return selected by the code, not an externally confirmed process
exit; unexpected exception types keep it null and propagate unchanged.

The signal handler records `STOP_REQUESTED` only in the bounded memory ring;
the next supervisor event persists it. The existing owned-child poll/terminate
behavior remains intact, including propagation of a termination race. No new
retry follows a supervision exception. When an exception propagates, an atomic
`DEGRADED` state is attempted with its phase, child PID and known exit code. Failure
of this write is recorded when possible and does not mask the initial exception.
The ordinary STARTING/STOPPED/DEGRADED transitions and retry delays remain intact.

Journal failure cannot create retries or change expected return codes. Evidence
is best effort: SIGKILL, a machine failure, denied access, full disks, or a blocked
filesystem can prevent or delay persistence. A signal without a subsequent event
may remain only in RAM. Atomic temporary files can survive an abrupt process or
machine death; normal writes clean them up, and this code does not delete unknown
files. The previous journal is retained for one further invocation, not forever.
No claim about actual Android state or acoustic delivery follows from these files.

Run `python -B -m unittest discover -s tests -p test_private_service.py` only in an
isolated environment with filesystem, process, signal and network guards installed
before imports. The tests use temporary roots, fake children, stored signal
callbacks and simulated time; they never start the real service.
