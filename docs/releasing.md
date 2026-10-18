# Releasing

The version lives only in `ai_bridge/__init__.py`. Clients see it in `system_info`
(`bridge_version`, `bridge_release`).

## Which part to bump

| Bump | When | Example |
|---|---|---|
| `major` | Breaks clients, config or stored state | Rename a tool or argument |
| `minor` | Adds something, breaks nothing | New tool or optional field |
| `patch` | Fixes, docs, tests | Bug fix |

Any change to `ai_bridge/` needs at least a patch bump, or activation refuses it.

## Steps

1. Note changes under `## [Unreleased]` in `CHANGELOG.md` as you work.
2. Test:
   ```bash
   ~/.local/share/ai-bridge/current/venv/bin/python -m unittest discover -s tests
   ```
3. Bump and check:
   ```bash
   python3 release.py bump patch    # or minor / major
   python3 release.py check
   ```
4. Commit and tag:
   ```bash
   git commit -am "Release X.Y.Z" && git tag -a vX.Y.Z -m "Release X.Y.Z"
   ```
5. Stage, activate, restart (see [operations](operations.md#updates-and-rollback)):
   ```bash
   bash install.sh --stage
   bash install.sh --activate '<RELEASE>'
   sudo systemctl restart ai-bridge
   ```
6. Check that `system_info` shows the new version, then `git push --follow-tags`.

## Activation rules

| New release vs live | `--activate` | `--rollback` |
|---|---|---|
| Higher version | ✅ | ✅ |
| Same version, same code | ✅ | ✅ |
| Same version, changed code | ❌ bump first | ✅ |
| Lower version | ❌ use `--rollback` | ✅ |

## What's deployed?

Each release folder is `<UTC time>-v<version>-<id>`. Its `RELEASE.json` records the
version, git commit, whether the checkout had uncommitted changes, and staging time.

```bash
cat "$(readlink -f ~/.local/share/ai-bridge/current)/RELEASE.json"
```

Releases from before versioning have no `RELEASE.json`; their version comes from the code.
