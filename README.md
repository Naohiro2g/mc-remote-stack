# mc-remote-stack

[日本語はこちら。](README_ja.md)

`mc-remote-stack` is the reproducible deployment and operations package for McRemote servers. It turns a new-design `mc-remote.toml`, or a transitional legacy `mc-remote.yml`, into validated, digest-pinned runtime configuration.

## Normal deployment: one file, two commands

Select a reviewed immutable preset and put the URLs and Minecraft target in one `mc-remote.toml`:

```toml
schema_version = 1
deployment = "school-a"
preset = "classroom@1"

[surfaces]
scratch_url = "https://scratch.example.org/"
bridge_url = "wss://bridge.example.org/"

[[targets]]
id = "classroom"
label = "Classroom"
sandbox = "minecraft.example.org"
default = true
```

The normal operator surface has two commands. `apply` performs validation, immutable preset resolution,
exact locking, artifact acquisition, rendering, and preflight, then derives create versus update from the
current exact lock, persistent world volume, and managed runtime state. It checks every required host port
before artifact acquisition or render publication and reports an external owner as a targeted conflict.
The fresh-host bootstrap installs `uv` at `$HOME/.local/bin/uv` and makes it
available by command name in subsequent login sessions.

```sh
uv run mcrctl apply ./mc-remote.toml
uv run mcrctl doctor school-a
```

`doctor` checks the exact container images, running and Minecraft health state, published ports, served
Scratch runtime schema and target set, Bridge allowlist and default target, Bridge-container-to-McRemote
reachability, and token-free `hello` rejection with `auth_required`.

The Scratch runtime schema, fixtures, container mount path, and Scratch image digest come from the contract
handoff locked by the preset; they are not additional operator inputs. `classroom@1` locks the unchanged contract
tree at Scratch commit `4c893bd…` and the Scratch/Bridge image digests published by Scratch's own CI. Stack only
checks the tag and registry manifest read-only and does not build Scratch, Bridge, or Plugin. Images from the
earlier workflow triggered by Stack are not referenced. Stack does not read product-config, Scratch source outside
the contract directory, or the unadopted `home-server@7` / `compose@15` prototype to infer fields.

The project is intentionally separate from:

- `mc-remote-knowledge`: public architecture and decision SSOT.
- `mc-remote-backstage`: private provider, contract, host, and incident operations; public users do not depend on it.
- a deployment project: instance-specific desired state and lock data.

## Operational runbooks

- [Agent-assisted bootstrap (Japanese)](docs/agent-assisted-bootstrap-guide_ja.md): the no-on-host-agent
  baseline, workstation-over-SSH assistance, and the security gate for limited on-host experiments
- [Fresh-host bootstrap (Japanese)](docs/fresh-host-bootstrap-guide_ja.md): prepare the individual
  administrator, SSH, exact Stack checkout, canonical uv, Python, Docker, and Compose
- [Public VPS bootstrap (Japanese)](docs/public-vps-bootstrap-guide_ja.md):
  the current same-volume release update from one reviewed handoff through plan, apply, and doctor
- [Normal dev environment (Japanese)](docs/normal-dev-environment-guide_ja.md): prepare and operate
  the shared server-side development environment
- [Home private alpha validation (Japanese)](docs/home-alpha-validation-guide_ja.md)
- [Wake-on-LAN operation (Japanese)](docs/wake-on-lan-field-note_ja.md): power-state operation for
  semi-always-on servers

## Design references

- [CLI validation environment plan (Japanese)](docs/cli-validation-environment-plan_ja.md):
  responsibilities across local development, a catering PC, a home server, and the running VPS
- [Catering-type validation roadmap (Japanese)](docs/catering-type-validation-roadmap_ja.md)
- [Deployment operator workflow redesign (Japanese)](docs/deployment-operator-workflow-design_ja.md):
  operator environment, code-first recovery value, release-independent durable update plans,
  live Compose provenance capture, limited rollback, and the 15-minute human-operation SLO
- [Preset and lock resolution design (Japanese)](docs/preset-resolution-design_ja.md): the next
  preset registry, preset catalog, compatibility-evidence, and lock-identity model; the bundled
  home profile/preset, typed operator input boundary, instance contract, and operator-facing TOML
  init/resolve/fetch/render path are implemented
- [TOML project layout design (Japanese)](docs/toml-project-layout-design_ja.md): one environment
  per project, no generic includes, owner separation, lossless editing, and the YAML/TOML coexistence gate;
  the isolated TOML project, explicit volume/world/network contract, `minecraft-motd@1`, and managed
  TOML render path are implemented
- [`home-beta` bootstrap apply design (Japanese)](docs/home-beta-bootstrap-apply-design_ja.md):
  current-lock and canonical-render binding, explicit local Docker context, managed initial volume,
  Compose startup, and container rollback; upgrades and existing-world reuse remain unsupported

## Development

```sh
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run mcrctl --help
```

## `home-beta` TOML operator path

Initialize exactly one environment with every instance identity explicit. The directory name does not
infer the environment identity or channel, and EULA acceptance, resolution, and artifact acquisition
remain separate operations. Keep the instance-specific order and lock in a deployment project outside
the package source checkout. TOML `init` caps the project root at mode `0750` and its initial files at
`0640`, while preserving a stricter caller umask.

```sh
MC_REMOTE_PROJECT="$HOME/mc-remote-deployments/home-beta"
uv run mcrctl init "$MC_REMOTE_PROJECT" \
  --format toml \
  --deployment-name home \
  --profile home-server@4 \
  --environment-identity home-beta \
  --channel beta \
  --exposure isolated \
  --purpose integration \
  --preset mcremote-paper@1 \
  --artifact-store "$HOME/.local/share/mc-remote/artifacts" \
  --volume minecraft-data=home-beta-minecraft-data \
  --world-identity home-beta-world \
  --bind-address 127.0.0.1 \
  --java-port 25565 \
  --mcremote-port 25575
```

To customize the public server-list text, add this optional reference to `mc-remote.toml`:

```toml
[[operator_inputs]]
role = "minecraft-motd"
adapter = "minecraft-motd@1"
path = "operator/minecraft-motd/server.properties"
```

Create `operator/minecraft-motd/server.properties` at the same time. This strict typed input is
public-display data only; never put a secret in it. Comment-only and whitespace-only changes do not
change the lock identity.

```properties
# Public server-list text
motd=McRemote home beta
```

Validate after adding any operator input and before resolving:

```sh
uv run mcrctl validate --project "$MC_REMOTE_PROJECT"
uv run mcrctl accept-eula --project "$MC_REMOTE_PROJECT" --yes
```

The exact `home-server@4` + `mcremote-paper@1` subject is unverified until its auth-enforced live
evidence is recorded. Record a specific unverified acknowledgement in the order before continuing:

```sh
uv run mcrctl resolve --project "$MC_REMOTE_PROJECT" --allow-unverified
uv run mcrctl plan --project "$MC_REMOTE_PROJECT"
uv run mcrctl artifact fetch --project "$MC_REMOTE_PROJECT"
uv run mcrctl render \
  --project "$MC_REMOTE_PROJECT" \
  --output "$MC_REMOTE_PROJECT/generated"
```

`artifact fetch` acquires only the HTTPS files named by the current lock, verifies every SHA-256, and
stores them at `<artifact_store>/sha256/<digest>`. It rehashes existing entries, does not pull the OCI
image, and never starts Compose. `render` likewise does not create volumes or contact a server.
For this exact verified subject, `plan` reports `compatibility=verified` and returns status 0. Apply
is not implicit: `render` still stops at managed generated output. A different exact profile,
preset, or component set remains unverified unless separately covered.

The first isolated `home-beta` can be bootstrap-applied on the target host through an explicit local
Unix-socket Docker context. Manually copy the reviewed `PLAN lock=unchanged identity=...` value;
do not derive it from ambient state.

```sh
REVIEWED_LOCK_IDENTITY="sha256:<reviewed-64-hex>"
uv run mcrctl apply \
  --project "$MC_REMOTE_PROJECT" \
  --output "$MC_REMOTE_PROJECT/generated" \
  --expected-lock-identity "$REVIEWED_LOCK_IDENTITY" \
  --docker-context default \
  --bootstrap \
  --yes \
  --allow-unverified
```

Apply pulls the exact OCI image, rejects unknown containers, unknown volumes, and port collisions,
then creates the managed world volume and starts Minecraft. A failed startup brings containers down
but retains the world volume. Docker installation, firewall mutation, existing-world import, and upgrades
are outside this command.

Use the read-only doctor after logging in instead of reusing apply as a status command:

```sh
uv run mcrctl doctor --project "$MC_REMOTE_PROJECT"
```

By default it checks `<project>/generated` through the local Docker context named `default`. It verifies
the current lock and generated bytes, the running containers' Compose provenance, managed volume,
container labels, running/healthy state, exact loopback port mappings, and that a token-free protocol
hello is rejected with `auth_required`. A successful token-free hello fails with
`doctor_auth_not_enforced`.
If runtime startup also used a Compose file other than the canonical generated `compose.yaml`, doctor
continues its runtime and protocol checks but reports `WARN render=additional-compose-files`. It does
not print container logs or session/player/token values. If another exact subject is unverified,
doctor retains an explicit warning even when its runtime is healthy.

Add `home-alpha` later as a separate initialized project with distinct volume and world identities;
do not copy the `home-beta` directory or lock.

## Public VPS deployment

The canonical public VPS procedure is [the public VPS release deployment runbook](docs/public-vps-bootstrap-guide_ja.md). A reviewed handoff supplies the target mapping, knowledge commit, Stack commit, deployment project, exact profile, exact preset, and authorized action. Follow its environment check, plan, apply, and doctor steps from top to bottom.

The deployment project `mc-remote.toml` and exact lock identify the active desired state. The handoff names the next exact set from `mc-remote-knowledge` release gate notes at its stated commit. A new Ubuntu host first completes the [fresh-host bootstrap](docs/fresh-host-bootstrap-guide_ja.md).

## Encrypted off-host backup transfer

The transfer adapter encrypts a ServerBackup archive with a public age recipient before opening an explicit FTPS session. It requires certificate verification, protects the data connection, uses passive mode, uploads through a temporary remote name, and verifies the final remote size. `--verify-download` additionally downloads the remote ciphertext and compares its SHA-256. A non-secret transfer-record sidecar is published with the ciphertext so recovery does not depend on the source VPS. Plaintext and encrypted local files remain in the queue; transfer does not prune them.

```sh
uv run mcrctl backup transfer /backup/outbox/backup.zip \
  --project ./deployment \
  --transport-config /secure/path/backup-transport.toml \
  --verify-download
```

For scheduled operation, create an activation marker so existing generations
are not selected implicitly. `drain` considers only archives newer than the
marker, at least 120 seconds old, valid under a full ZIP CRC check, and
unchanged in identity, size, and mtime while checked. Every selected archive is
downloaded again after upload and its ciphertext SHA-256 is verified. An
archive with a local `download-verified` transfer record is not sent again.

```sh
install -m 600 /dev/null /secure/state/backup-transfer-activated

uv run mcrctl backup drain /backup/outbox \
  --after /secure/state/backup-transfer-activated \
  --project ./deployment \
  --transport-config /secure/path/backup-transport.toml
```

Creating the marker and registering a persistent timer are operator
checkpoints. Create the marker once, after inspecting existing archives and
before the first automatic run. `drain` does not delete plaintext archives,
local ciphertexts, transfer records, or remote generations. Decide local queue
retention explicitly and separately from snapshot generation retention.

TOML deployments keep provider/account inventory in a separate private mode-`0600`
transport file. Legacy YAML deployments may continue to use their embedded transitional
transport table. Neither form contains the password value.

Recovery selection is always explicit. List completed ciphertexts, retrieve the selected
record and archive, then decrypt and verify the original plaintext SHA-256:

```sh
uv run mcrctl backup list --project ./deployment \
  --transport-config /secure/path/backup-transport.toml

REMOTE_NAME='backup.zip.<encrypted-sha256>.age'
uv run mcrctl backup download-record "$REMOTE_NAME" \
  --project ./deployment \
  --transport-config /secure/path/backup-transport.toml \
  --output ./recovery/backup.transfer.json
uv run mcrctl backup download "$REMOTE_NAME" \
  --project ./deployment \
  --transport-config /secure/path/backup-transport.toml \
  --record ./recovery/backup.transfer.json \
  --output ./recovery/backup.zip.age
uv run mcrctl backup decrypt ./recovery/backup.zip.age \
  --record ./recovery/backup.transfer.json \
  --identity /secure/path/age-identity.txt \
  --output ./recovery/backup.zip
uv run mcrctl archive inspect ./recovery/backup.zip --json
```

In `backup list`, `record=present` means the ciphertext has its remote recovery sidecar.
`record=missing` identifies a legacy or incomplete transfer that cannot start
`download-record` from the remote endpoint alone. The entry remains visible, but the CLI does
not claim it is recoverable.

The commands never choose “latest,” delete a remote generation, overwrite an existing
local output, or print the FTPS password or age identity. Keep the age identity outside
the deployment project and Git.

The FTPS password is referenced as `secret://backup_ftps_password` and stored with `mcrctl secret set`; it is never placed in the deployment project. A VPS-only user can instead download and upload outbox artifacts over the existing SSH/SFTP path. The package does not install an FTP daemon on the VPS. A snapshot that exists only on the VPS is local recovery state, not an off-host backup.

Inspect an existing whole-server recovery point without extracting its secret-bearing contents:

```sh
uv run mcrctl archive inspect /path/to/backup.zip --json
```

The result contains the archive SHA-256, ZIP CRC result, aggregate sizes, region count, root server JAR identities, and active `plugins/*.jar` SHA-256 values. Nested Paper remap caches and plugin libraries are counted but not misreported as active plugins. It does not print plugin configuration contents.
Plugin-declared Paper runtime library coordinates are reported as
`runtime_libraries`; this inventories a download declaration but does not claim that
the transitive content is locked.

Restore only the selected world roots into a current TOML deployment:

```sh
uv run mcrctl world restore plan ./recovery/backup.zip \
  --project ./deployment \
  --output ./deployment/generated \
  --source-world world \
  --expected-archive-sha256 '<64-lowercase-hex>' \
  --expected-lock-identity 'sha256:<64-hex>'

uv run mcrctl world restore apply ./recovery/backup.zip \
  --project ./deployment \
  --output ./deployment/generated \
  --source-world world \
  --expected-archive-sha256 '<64-lowercase-hex>' \
  --expected-lock-identity 'sha256:<64-hex>' \
  --yes
```

The transaction rejects unsafe or duplicate ZIP entries and symlinks, stages only
the overworld and present Nether/End roots, stops only Minecraft for cutover, starts
the current locked service, and runs doctor. Plugin data and credentials are not
extracted. A failed start or doctor check restores the prior roots. A successful
transaction retains the prior roots in the reported rollback directory until the
operator completes validation. Apply also rejects a container started with additional
Compose files because restarting it from the canonical render could silently remove
services, mounts, or plugins supplied by an override.

This is a write-set contract for the repository-managed recovery commands. `world
restore` writes only the selected world roots in the managed Minecraft data volume;
`artifact import-archive` writes only lock-named JAR bytes to the content-addressed
artifact store. Neither command writes `plugins/McRemote/`. Manual or temporary plugin
data recovery is a separate operation and is not covered by the world-restore contract.

The current `@server` whole-server archive includes plugin data under `/data` and is
security-sensitive runtime state. The transfer adapter sends it off-host only as
age-encrypted ciphertext; keep plaintext retention, recipient access, and the age
identity under explicit operator control. Archive inclusion does not make plugin data
part of the world-restore contract.

The credential-separated `home-server@3` / `compose@5` profile mounts the credential
snapshot and revocation authority in independent volumes outside `/data`. This excludes
both from world restore and an archive limited to `/data`. The exact b3 preset and the
isolated-alpha `mcremote-paper@6` candidate with persistent session tokens are implemented.
The exact `@6` McRemote JAR SHA-256 is
`331633ef15a729658496e89fe49cb8a5eb5ebcb2ec86937b7e5313528d7ec997`.
Controlled bootstrap is limited to the `alpha` / `isolated` / `integration` combination.
Home-alpha validation covered fresh credential bootstrap, session reuse after restarting the
same b4 runtime, and replaying saved Scratch and Python building code on a fresh world after
new pairing.

The nonce-bound machine-readable plugin checkpoint and doctor consumer, general bootstrap
and reset transactions, and the public long-lived-credential gate remain a later slice.
Doctor currently fails closed with `doctor_credential_health_unsupported` after validating
the mount topology. This does not approve the profile as a public default, but the separate
credential-lifecycle work does not block the b4 user-facing feature gate, in accordance with
the knowledge authentication roadmap.

Classify explicit runtime dependency downloads and update checks from a startup log
without reproducing raw log lines or URL paths:

```sh
uv run mcrctl runtime audit-log ./minecraft-startup.log --json
```

This diagnostic recognizes Paper library downloads, Geyser-style runtime content
downloads, and update checks. Absence of a matching event does not prove that a
plugin made no network request.

Import only the Paper and plugin JAR members named by a deployment lock from a recovery archive:

```sh
uv run mcrctl artifact import-archive /path/to/backup.zip --project ./deployment
```

The command verifies the whole archive SHA-256, requires each named member to exist exactly once, verifies each artifact SHA-256 while streaming, and writes only those JARs to a content-addressed local store. It does not extract world data or plugin configuration. `MC_REMOTE_ARTIFACT_HOME` can relocate the local store; `--store` selects an explicit SHA-256 store directory.

Rendered Minecraft Compose configuration mounts the locked Paper JAR through `PAPER_CUSTOM_JAR` and mounts each locked plugin read-only through the image's `/plugins` attach point. Startup removes only top-level old JARs from `/data/plugins` before synchronizing the locked set, so plugin data directories remain while stale renamed JARs are removed. Runtime download of Paper defaults is disabled; generated configuration is synchronized from `/config`.

The official profile also renders `mc-remote.com` and `www.mc-remote.com` as a Caddy static site. Homepage content is mounted read-only from `/var/lib/mc-remote/homepage/sha256/<sha256>`, while certificates, private keys, and ACME state remain in Caddy's separate `/data` volume. A recovered rental-server archive may use `source_archive` provenance to record its source SHA-256, source root, and intentionally excluded host-specific files.
