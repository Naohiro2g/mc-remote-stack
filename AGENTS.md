# Agent Guide: mc-remote-stack

Before changing McRemote architecture, deployment behavior, protocol-facing configuration, learning design, or
security policy, read the relevant documents in `Naohiro2g/mc-remote-knowledge`, especially:

- `40-サービス運用/server-package-design_ja.md`
- `40-サービス運用/server-topology-design_ja.md`
- the applicable rows in `00-hub/DECISIONS_ja.md`

First, load only the latest dev agent runtime protocol from the knowledge repository's remote `main`.
Do not print the entire source file into the conversation.

```bash
protocol_source="$(mktemp)"
knowledge_commit="$(gh api repos/Naohiro2g/mc-remote-knowledge/commits/main -q .sha)"
gh api "repos/Naohiro2g/mc-remote-knowledge/contents/00-hub/dev-repo-protocol_ja.md?ref=$knowledge_commit" \
  -q .content | base64 -d > "$protocol_source"
if [ "$(grep -Fxc '<!-- BEGIN: DEV-AGENT-RUNTIME -->' "$protocol_source")" -ne 1 ] || \
   [ "$(grep -Fxc '<!-- END: DEV-AGENT-RUNTIME -->' "$protocol_source")" -ne 1 ]; then
  echo "dev agent runtime marker missing or duplicated" >&2
  exit 1
fi
printf 'knowledge commit: %s\n' "$knowledge_commit"
awk '/^<!-- BEGIN: DEV-AGENT-RUNTIME -->$/{reading=1;next} \
     /^<!-- END: DEV-AGENT-RUNTIME -->$/{reading=0} \
     reading' "$protocol_source"
```

- Related knowledge spokes: `40-サービス運用/`, `11-plugin/`, `13-scratch-client/`, `14-evidence/`

If the SSOT is unavailable, stop. Do not infer McRemote-specific decisions from this repository alone.

Keep the operator path simple: one deployment project layout, English configuration keys, secrets outside Git,
immutable artifact identities, and explicit plan/apply boundaries. Add tests before fixing bugs. Do not add a
fallback that weakens security or silently changes a deployment profile.

Run before submitting:

```sh
uv sync --extra dev
uv run pytest
uv run ruff check .
```
