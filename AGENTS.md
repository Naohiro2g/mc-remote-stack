# Agent Guide: mc-remote-stack

This repository is the SSOT for the publishable McRemote server package and the public runbooks that must match
its current implementation. Before changing cross-repository architecture, protocol-facing configuration,
learning design, evidence policy, or security policy, read the relevant documents in
`Naohiro2g/mc-remote-knowledge`, especially:

- `10-protocol/`
- `11-plugin/`
- `13-scratch-client/`
- `14-evidence/`
- `20-教材/` when changing learning or user-assistance design
- `00-hub/llm-agent-boundary-guide_ja.md` when changing agent authority or execution boundaries
- the applicable rows in `00-hub/DECISIONS_ja.md`

Do not depend on the frozen knowledge archive. Private provider/account/cost/inventory operations belong in
`mc-remote-backstage` and are not required for public contributor work.

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

- Related knowledge spokes: `10-protocol/`, `11-plugin/`, `13-scratch-client/`, `14-evidence/`

If the SSOT is unavailable, stop. Do not infer McRemote-specific decisions from this repository alone.
Treat `mc-remote-knowledge` as read-only while working in this repository. Do not edit, commit, or push that
repository. When evidence or a cross-repository decision needs to land there, prepare a sanitized draft and hand
it off to the repository owner through the approved review workflow.

Keep the operator path simple: one deployment project layout, English configuration keys, secrets outside Git,
immutable artifact identities, and explicit plan/apply boundaries. Add tests before fixing bugs. Do not add a
fallback that weakens security or silently changes a deployment profile.

When assisting an actual operator with initial host setup, read
`docs/agent-assisted-bootstrap-guide_ja.md`. The baseline runbook must remain completable by a human in the target
terminal without installing an agent on the target host. A terminal-capable agent on the administrator workstation
may assist over SSH, but remote commands still have the SSH user's authority and require explicit mutation
boundaries. An agent running on the target host is an experimental security-evaluation path, not a co-equal
canonical mode or a prerequisite. Do not install or start one on an existing administrator account as part of
ordinary bootstrap.

If the user explicitly evaluates an on-host agent, use a rebuildable host and a dedicated unprivileged OS user
with no `sudo`, rootful `docker` group, SSH agent forwarding, personal credentials, or access to important world
data. Keep the agent in read-only or workspace-write with interactive human approvals. Have the human run Docker,
sudo, apply, and doctor steps from a separately owned trusted checkout and deployment project, then return
sanitized output. Never have an administrator execute code, a virtual environment, or generated Compose files
writable by the experimental agent user. Do not weaken the sandbox, add broad Unix-socket access, or silently
switch the deployment to Rootless Docker.

Choose assistance from observed tool and permission boundaries, not from a model name. Start with read-only
discovery and do not require a clean OS reinstall. Do not assume `mcrctl` is on `PATH`; during bootstrap use the
validated checkout's `uv run mcrctl` or exact `.venv/bin/mcrctl`.

Keep the human in control of target selection, alternate SSH/sudo verification before hardening, EULA acceptance,
the written reason for unverified artifacts, plan/lock review, apply confirmation, pairing, and any persistent
tool installation or PATH change. An agent may execute approved deterministic steps and checks, but it must not
turn those human checkpoints into implicit defaults. Separate public-safe progress from private host inventory and
secret-bearing raw output, and leave a restartable handoff when the work pauses.

Run before submitting:

```sh
uv sync --extra dev
uv run pytest
uv run ruff check .
```
