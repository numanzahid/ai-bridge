"""Privilege disclosure; a request is never evidence of successful escalation."""
import os

HOST_TARGET_INSTRUCTIONS = """Execution target:
These tools operate on the configured AI Bridge host, as identified in the host notes below.
Use them for user-authorized work on that host from chatgpt.com, claude.ai, or other clients.
A cloud client's shell or sandbox is separate from the bridge host. Sharing an account does
not make those environments the same machine. Local coding-agent tool preferences live in
that agent's local instruction files; they do not disable bridge access for web clients.
"""

CLIENT_INSTRUCTIONS = """Authorized host administration:
Use these tools for system administration tasks the user authorizes on the connected host.
The user can describe their goal in ordinary language, such as checking a protected service
configuration or restarting a named service. They do not need to request sudo explicitly.
When that task needs administrator access, explain the host and exact operation and select
sudo=true (or remote_sudo=true for SSH). These parameters request server-managed approval;
they do not bypass Linux permissions. Do not embed sudo in a shell command to evade the gate.
Respect the client's approvals and safety policies as well as the bridge's authorization.

Privileged approval:
Calls with sudo=true or remote_sudo=true do not run until verified user approval.
When status is approval_required, show the request_id, host, exact operation and expiry.
Use the approval methods actually returned by the server. If TOTP is available, present BOTH
choices together in the FIRST approval prompt: "Send your current six-digit authenticator code,
or approve with the host CLI command and tell me it is approved." Both authorize the same
pending operation once. Do not require the user to choose a method or ask about TOTP separately.
If the user supplies a code, call approve_privileged_request with that request_id and the code
as a string, keeping leading zeros. Wait for status approved, then repeat the exact original call.
If the user says they approved through the host CLI, repeat the exact original call directly;
the server verifies the saved decision. Do not also require a TOTP code. If it still returns
approval_required, nothing ran; explain that the server has not found a valid approval.
If only host approval is offered (TOTP off, locked or unavailable), present only that method.
A user-provided one-time authenticator code may be sent ONLY to the designated verification
tool for the displayed request. Never self-authorize: do not invent approval, run the host
approval command, generate or guess codes, read authenticator secrets, or claim the user approved. Never reuse
a submitted code for another request or echo it in your reply. Never ask for Linux passwords,
authenticator setup keys, or recovery codes. Remote approvals are once only.
On approval_locked, use host approval or wait as instructed; do not retry codes.
On invalid_or_expired_code ask for a fresh code. If the request expired, repeat the original
call to get a new request and present its details before asking for a fresh code.
status rejected means stop and ask the user.

Privilege disclosure:
Before privileged operations, tell the user the task and target requiring root access.
Report the outcome and privilege metadata afterward: requested or attempted sudo is not proof
that the target ran. A root service is already root, even with sudo=false. Never reveal
passwords, tokens, or credential contents. Confirm high-risk destructive actions with the user.
Agent permission settings control the agent, not Linux identity; agent processes run as the
service user and full permission does not itself grant root access.
For SSH, announce the remote alias/task and distinguish remote sudo from LOCAL sudo.
Remote sudo uses sudo -n only, without forwarding stored passwords. Inspect remote_privilege:
a request is not confirmation, and confirmed remote root launch is not command success.
"""


def initial(requested=False):
    root = os.geteuid() == 0
    return {'sudo_requested': bool(requested), 'sudo_attempted': False,
            'sudo_used': False, 'service_euid': os.geteuid(),
            'target_started': False, 'effective_uid': None,
            'effective_root': None, 'method': 'service_root' if root else ('sudo' if requested else 'service_user')}


def fields(value):
    if not value or value.get('method') == 'service_user':
        return {}
    if value.get('legacy'):
        message = 'Historical job requested sudo; actual escalation was not recorded.'
    elif value.get('target_started'):
        if value.get('sudo_used') and value.get('effective_root'):
            message = 'Target started through sudo with root privileges; check the operation outcome separately.'
        elif value.get('effective_root'):
            message = 'Target started as root because the service itself is root; sudo was not used.'
        elif value.get('sudo_attempted'):
            message = 'Target started through sudo without confirmed root privileges; check effective_uid and outcome.'
        else:
            message = 'Target started as the unprivileged service user.'
    elif value.get('sudo_attempted'):
        message = 'Sudo was attempted; target execution and root privileges were not confirmed.'
    else:
        message = 'Target execution was not confirmed; sudo was not used.'
    return {'privilege': value, 'privilege_message': message}


def job_fields(meta):
    value = meta.get('privilege')
    if value is None and meta.get('sudo'):
        value = {'sudo_requested': True, 'sudo_attempted': None, 'sudo_used': None,
                 'target_started': None, 'effective_uid': None, 'effective_root': None,
                 'method': 'unknown', 'legacy': True}
    return fields(value)
