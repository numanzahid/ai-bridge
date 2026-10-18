"""Privilege disclosure; a request is never evidence of successful escalation."""
import os

CLIENT_INSTRUCTIONS = """Privileged approval:
Calls with sudo=true or remote_sudo=true do not run until the user approves them on the host.
When a result has status approval_required, show the user the request_id, host and operation
and ask them to approve it on the host. Only after they say they approved, repeat the exact
same call. Never approve requests yourself, run the approval command, or claim the user approved.
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
