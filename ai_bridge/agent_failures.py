"""Small, best-effort classification of failed native-agent diagnostics."""

import re


def describe_failure(diagnostics: str, status: str) -> dict:
    # Inspect error diagnostics only, never successful replies or tool output.
    # Unknown wording stays unknown rather than requiring a private CLI schema.
    rules = (
        ('usage_limit', r"out of usage|usage limit (?:reached|exceeded)|quota (?:exceeded|exhausted)|insufficient_quota",
         'blocked', 'Check account usage/billing or choose an available model explicitly before retrying.'),
        ('rate_limit', r'rate.?limit|too many requests|\b429\b',
         'blocked', 'Wait for the provider limit to clear before retrying; no reset time is inferred.'),
        ('authentication_required', r'unauthorized|authentication (?:failed|required)|invalid api key|not logged in|please (?:log|sign) in',
         'blocked', 'Check the native CLI login or credentials before retrying.'),
        ('service_unavailable', r'service unavailable|temporarily unavailable|overloaded|internal server error|\b(?:502|503|504)\b|connection refused|connection reset|network error|fetch failed',
         'failed', 'Check provider/network availability, then retry explicitly if appropriate.'),
    )
    if status == 'cleanup_incomplete':
        code, outcome, action = 'cleanup_incomplete', 'interrupted', 'Inspect remaining processes locally before continuing or retrying.'
    elif status in ('killed', 'lost'):
        code, outcome, action = 'interrupted', 'interrupted', 'Inspect the job and existing session before deciding whether to continue.'
    else:
        code, outcome, action = 'execution_failed', 'failed', 'Inspect the diagnostics before retrying; the cause is not recognized.'
        for candidate, pattern, result, advice in rules:
            if re.search(pattern, diagnostics, re.IGNORECASE):
                code, outcome, action = candidate, result, advice
                break
    return {
        'outcome': outcome,
        'failure': {'scope': 'agent', 'code': code,
                    'message': diagnostics[-2000:].strip() or 'Agent ended without a recognized error message.',
                    'automatic_retry': False},
        'next': action + ' No automatic retry or replacement chat was started.',
    }
