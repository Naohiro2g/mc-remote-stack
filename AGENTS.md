# Agent Guide: mc-remote-stack

Before changing McRemote architecture, deployment behavior, protocol-facing configuration, learning design, or
security policy, read the relevant documents in `Naohiro2g/mc-remote-knowledge`, especially:

- `40-サービス運用/server-package-design_ja.md`
- `40-サービス運用/server-topology-design_ja.md`
- the applicable rows in `00-hub/DECISIONS_ja.md`

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
