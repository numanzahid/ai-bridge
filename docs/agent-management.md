# Agent management from the host

After selecting a verified release, register the stable launcher explicitly from
a trusted host terminal **as the service account**, with its normal HOME:

```bash
bash /path/to/source/install.sh --install-cli
export PATH="$HOME/.local/bin:$PATH"
ai-bridge agents status
```

A dedicated installation can use its accessible bootstrap source copy or
`~/.local/share/ai-bridge/current/validation/install.sh`. For a root service run
as root; for a dedicated service switch to that account first. No sudo, bootstrap,
activation or restart occurs during registration. Existing foreign commands,
symlinks and changed launchers are refused. Keep `~/.local/bin` on your shell PATH.

Owner-owned home/`.local`/`bin`/`share`/deployment-root ancestors may be 0775 only
when their group is the account's private primary group: passwd/group enumeration
must identify the account/group and no other primary or supplementary members.
Unknown membership or ACL state fails closed; extended access/default ACLs are
refused for this exception. World-write, shared-group write and symlinks
remain refused; release directories and executable files still reject group-write.
No existing directory permissions are changed.

The owned launcher follows `~/.local/share/ai-bridge/current` and pins one release
per invocation. The selector may be owned by the service user or root (privileged
activation); the selected release, app, environment and READY marker must remain
service-user-owned. Activation/rollback do not change the launcher. If an older release lacks
management support it fails clearly; it never falls back to another release or
starts the server. Changed launcher source is refused rather than silently replacing
the installed copy; a future launcher migration needs explicit review.

Management needs neither an MCP token nor a running server. Use the same
`AI_BRIDGE_STATE_DIR` and `AI_BRIDGE_TASKS_DIR` as the service if customized; the
CLI does not load `/etc` credentials. `python -m ai_bridge <management-command>`
remains supported from the app directory with its venv; without arguments that
module starts the server. Bare `ai-bridge` instead displays CLI help.

## Terminal output and scripting

Every management subcommand accepts `--json` and `--table`. They also work before
its group or action, for example `ai-bridge --json agents status` or
`ai-bridge sessions --table list`. Combining different formats is an error.
Without a flag, stdout attached to a terminal uses human-readable output;
redirected or piped stdout keeps the existing JSON data shapes. Scripts should
use `--json` explicitly when a terminal might be attached.

```bash
ai-bridge sessions list                         # compact terminal table
ai-bridge sessions list --json                  # full IDs and metadata
ai-bridge sessions list --table > sessions.txt  # readable report, no ANSI color
ai-bridge agents status --json
ai-bridge tasks inspect task-<24-hex-digits> --table
```

Lists show totals; sessions also show the current offset, filtered total and
next offset when another page exists. Keep the same filters and limit when
requesting the next page. Unavailable agents and unreadable session records
produce visible warnings; totals cover only successfully enumerated records.
An explicitly selected unavailable agent still fails. Bridge status describes
bridge jobs, not external agent activity.

Session UUIDs are shortened to eight characters in lists. These are display
hints, not accepted selectors and not guaranteed unique. Use `--json` to obtain
the full UUID, then `sessions inspect agent:<full-UUID>` for every saved field
and an exact, copyable `selector:` line. Inspect/detail output retains full IDs
and nested fields. Long table values are ellipsized; use inspect or JSON for
complete values. Terminals narrower than 70 columns use wrapped records instead
of columns. Full identity lines in detail output intentionally remain unbroken,
even when wider than the terminal.

Timestamps in lists are relative (native timestamps without a timezone are
interpreted in the host's local timezone); inspect and JSON retain original
values. Unicode display widths account for wide characters and combining marks;
complex emoji sequences can vary by terminal. Stored terminal control characters
are escaped in human-readable output. Simple status colors appear only on TTY
stdout; setting `NO_COLOR` (even empty) or `TERM=dumb` disables them. Piped output
never contains generated ANSI colors, including with `--table`.

Operational errors remain JSON on stderr with exit code 1, including in table
mode. Invalid arguments retain exit code 2 and use JSON on stderr when stdout is
not a TTY; terminal argument errors retain argparse usage help. Success remains
exit code 0. If table formatting fails after a successful action, the CLI emits
the JSON result on stdout and exits 0 without repeating the action. Help is
always human-readable. Format flags affect presentation
only; interactive opening still requires a real terminal and all ownership,
cleanup and cancellation checks still apply.

## Tasks and turns

```bash
ai-bridge tasks create
ai-bridge tasks list
ai-bridge tasks inspect task-<24-hex-digits>
ai-bridge sessions start --agent codex 'Investigate this question'
ai-bridge sessions start --agent claude --cwd /path/to/project 'Review this project'
ai-bridge sessions start --agent cursor --task-id task-<24-hex-digits> 'Review shared work'
```

A new turn without cwd/session/task gets a unique 0700 workspace under
`~/.ai-bridge-tasks/`. Explicit projects remain in place. Multiple agents may
share a task; results warn about concurrent bridge jobs in that workspace.
Coordinate edits, use readonly review, or create separate tasks. No file locking
or isolation is imposed on agent tools. Metadata records task/job/workspace linkage;
native CLIs continue owning authentication, settings and histories.

Tasks have **no automatic expiry**. To remove generated files deliberately:

```bash
ai-bridge tasks cleanup task-<24-hex-digits> --dry-run
ai-bridge tasks cleanup task-<24-hex-digits> --yes
```

Cleanup requires an exact ID and matching private ownership marker. It removes
that workspace and marker only, never native history. It refuses active/uncertain
jobs, observed native processes using the workspace, uninspectable same-user
processes, mounted subtrees, symlinked management paths or unsafe permissions. Preview shows blockers.
Internal file symlinks are removed without following targets. Keep the host
quiescent: locks serialize task cleanup and bridge agent starts, not arbitrary
same-user writers, shell commands or external agents. Missing/tampered markers require manual inspection, not adoption.
Task workspaces remain after uninstall; clean desired tasks before uninstalling.

For unreadable `/proc/PID/cwd` observations only, an explicit **high-risk** opt-in
is available after reviewing the host from a trusted terminal:

```bash
ai-bridge tasks cleanup task-<24-hex-digits> --dry-run --allow-uninspectable
ai-bridge tasks cleanup task-<24-hex-digits> --yes --allow-uninspectable
```

Results enumerate `ignored_uninspectable_pids`, the risk and remaining `blockers`.
Dry-run never deletes; removal requires both flags. Those processes may still use
the task's files. This exception never ignores known active processes, uncertain
managed jobs, mounts, changed ownership/state or unsafe paths. Native history is
never deleted and no process is stopped. Default cleanup remains conservative.

## Runtime and stopping

```bash
ai-bridge agents status
ai-bridge agents inspect <exact-job-id>
ai-bridge agents stop <exact-job-id>
```

Only bridge-managed agent jobs are controlled. Saved sessions and unrelated native
services are not process handles. Runner/target PID identities, observed descendants (up to 256 identities),
runner RSS/CPU ticks and cgroup information are best-effort Linux observations.
`--timeout <seconds>` on start/continue sets a runner deadline (1..604800);
omitting it leaves the turn unlimited. Runners survive bridge restarts without replay.

Stop requests cancellation through a verified runner, then checks completion.
Agent cancellation signals verified processes, without process-group broadcasts.
Recorded same-boot descendants may be signalled only after identity and cgroup
checks. No process-name killing or mandatory systemd scope is used. Sampling can
miss rapidly detached or privileged children: cancellation retains
`cleanup_incomplete` rather than claiming certified whole-tree cleanup. Inspect
locally before retrying or cleaning a blocked task. Stop does not undo file edits.
Legacy records without identities remain readable but unsafe cancellation is refused.

## Saved sessions

```bash
ai-bridge sessions list --all --agent codex --mode noninteractive
ai-bridge sessions list --type subagent --project /path/to/project
ai-bridge sessions inspect codex:<full-session-uuid>
ai-bridge sessions continue codex:<full-session-uuid> 'Continue the investigation'
ai-bridge sessions open codex:<full-session-uuid> --dry-run
ai-bridge sessions open codex:<full-session-uuid>
```

Use `agent:UUID` or a full UUID plus `--agent`; partial IDs/names never select a
chat. List also accepts `--task-id`, `--status`, `--limit` and `--offset`.
`--all` includes bridge-archived records; all modes are otherwise visible.
Mode/provenance comes from recognized saved metadata, with `mode_scope`; it is
not the current terminal/frontend. Codex reports origin; a saved SDK marker
identifies headless activity in the scanned transcript prefix. Missing evidence stays
unknown (including Cursor execution mode). Bridge status is `active_job`,
`completed`, `idle` or `uncertain`; it does not certify external native inactivity.

Open uses the original cwd, storage ownership checks and installed CLI help
preflight. Actual execution requires stdin/stdout attached to a real terminal;
never invoke interactive open through MCP. Codex uses `resume
--include-non-interactive <UUID>`; the other adapters use `--resume <UUID>`.
Native resume can still reject incompatible state; preflight is not a conversion
guarantee. `promote` is the same native interactive opening, with no metadata
rewrite or promise about future picker visibility. `fork` uses native Codex fork
only when installed help supports the requested mode; other cases are unsupported.

```bash
ai-bridge sessions rename codex:<full-session-uuid> 'Display alias'
ai-bridge sessions archive codex:<full-session-uuid>
ai-bridge sessions unarchive codex:<full-session-uuid>
```

Rename/archive are private **bridge overlays** under state/management. Native
transcripts, titles, parent relationships, JSONL and SQLite stores are unchanged.
There is no transcript edit, conversion or native-history delete command. Revert
an overlay with another rename or unarchive; native clients ignore these overlays.
