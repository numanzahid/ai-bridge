"""Fit tool payloads before serialization, preserving valid JSON and control fields."""
import json


def render(payload, limit, redact):
    limit = max(1024 if isinstance(payload, dict) and ('privilege' in payload or 'remote_privilege' in payload) else 512, limit)
    if isinstance(payload, str):
        text = redact(payload)
        if len(text) <= limit:
            return text
        return text[:limit//2]+'\n...[text truncated]...\n'+text[-max(0,limit//2-30):]
    # Serialize/redact first so replacement never breaks escape sequences in values.
    # Redactor replacements are plain ASCII and valid inside JSON strings.
    def scrub(item):
        if isinstance(item, str):
            return redact(item)
        if isinstance(item, list):
            return [scrub(v) for v in item]
        if isinstance(item, dict):
            return {redact(str(k)): scrub(v) for k, v in item.items()}
        return item
    value = scrub(json.loads(json.dumps(payload, ensure_ascii=False, default=str)))
    encode = lambda v: json.dumps(v, ensure_ascii=False, indent=1)
    if len(encode(value)) <= limit:
        return encode(value)
    changed = set()
    def shrink(v, budget, path=''):
        if isinstance(v, str) and len(v) > budget:
            changed.add(path)
            return v[:budget//2]+' ...[cut]... '+v[-budget//2:]
        if isinstance(v, list):
            if len(v) > max(1, budget//128):
                changed.add(path)
                v = v[:max(1,budget//128)]
            return [shrink(item, budget, path+f'[{i}]') for i,item in enumerate(v)]
        if isinstance(v, dict):
            return {k: shrink(item, budget, path+'.'+k) for k,item in v.items()}
        return v
    budget = max(64, limit//3)
    while True:
        value = shrink(value, budget)
        result = dict(value) if isinstance(value, dict) else {'items': value}
        result['truncation'] = {'truncated': True, 'fields': sorted(changed)[:12]}
        text = encode(result)
        if len(text) <= limit:
            return text
        if budget == 64:
            # Very large metadata shapes: preserve known control fields, never
            # splice serialized JSON. Normal tool schemas do not need this fallback.
            keys = ('job_id','agent','status','outcome','session_id','exit_code','is_error','failure','error','next','next_offset','privilege','privilege_message','remote_privilege','remote_privilege_message','host','method','committed','staging_cleanup')
            result = {k: shrink(value[k],64,k) for k in keys if isinstance(value,dict) and k in value}
            result['truncation'] = {'truncated': True, 'metadata_omitted': True}
            if len(encode(result)) > limit:
                result = {'error': 'Result metadata exceeds response limit.', 'truncation': {'truncated': True},
                          **({k: value[k] for k in ('privilege', 'privilege_message', 'remote_privilege', 'remote_privilege_message', 'host', 'method') if k in value} if isinstance(value, dict) else {})}
            return encode(result)
        budget = max(64, budget//2)
