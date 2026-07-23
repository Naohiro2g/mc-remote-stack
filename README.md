# mc-remote-stack

[日本語はこちら。](README_ja.md)

`mc-remote-stack` is the reproducible deployment and operations package for McRemote servers. It turns a new-design `mc-remote.toml`, or a transitional legacy `mc-remote.yml`, into validated, digest-pinned runtime configuration.

The project is intentionally separate from:

- `mc-remote-knowledge`: public architecture and decision SSOT.
- `mc-remote-backstage`: private provider, contract, host, and incident operations; public users do not depend on it.
- a deployment project: instance-specific desired state and lock data.

## Public runbooks

- [Fresh-host bootstrap (Japanese)](docs/fresh-host-bootstrap-guide_ja.md)
- [Legacy server-runbook migration notes (Japanese)](docs/server-runbook-migration-notes_ja.md)
- [Preset and lock resolution design (Japanese)](docs/preset-resolution-design_ja.md): the next
  preset registry, preset catalog, compatibility-evidence, and lock-identity model; the bundled
  home profile/preset, typed operator input boundary, instance contract, and operator-facing TOML
  init/resolve/fetch/render path are implemented; apply remains unimplemented
- [TOML project layout design (Japanese)](docs/toml-project-layout-design_ja.md): one environment
  per project, no generic includes, owner separation, lossless editing, and the YAML/TOML coexistence gate;
  the isolated TOML project, explicit volume/world/network contract, `minecraft-motd@1`, and managed
  TOML render path are implemented, while plugin-specific mappings and apply remain unimplemented

The legacy repository's native-systemd, package-Caddy, and release-symlink procedures are not current instructions:
they conflict with this repository's Compose and generated-configuration architecture.

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
remain separate operations.

```sh
uv run mcrctl init ./deployments/home-beta \
  --format toml \
  --deployment-name home \
  --profile home-server@2 \
  --environment-identity home-beta \
  --channel beta \
  --exposure isolated \
  --purpose integration \
  --preset mcremote-paper@1 \
  --artifact-store /var/lib/mc-remote/artifacts \
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
uv run mcrctl validate --project ./deployments/home-beta
uv run mcrctl accept-eula --project ./deployments/home-beta --yes
```

The bundled `mcremote-paper@1` revision remains `unverified` until its compatibility evidence is
recorded. For a deliberate bootstrap, a human must set
`acknowledgements.allow_unverified = true` and a concrete `unverified_reason` in `mc-remote.toml`,
then supply the one-shot flag:

```sh
uv run mcrctl resolve --project ./deployments/home-beta --allow-unverified
uv run mcrctl plan --project ./deployments/home-beta
uv run mcrctl artifact fetch --project ./deployments/home-beta
uv run mcrctl render \
  --project ./deployments/home-beta \
  --output ./deployments/home-beta/generated
```

`artifact fetch` acquires only the HTTPS files named by the current lock, verifies every SHA-256, and
stores them at `<artifact_store>/sha256/<digest>`. It rehashes existing entries, does not pull the OCI
image, and never starts Compose. `render` likewise does not create volumes or contact a server.
While the preset is unverified, `plan` prints the plan and warning and returns status 1. Apply is not
implemented, so the supported path stops at managed generated output.

Add `home-alpha` later as a separate initialized project with distinct volume and world identities;
do not copy the `home-beta` directory or lock.

## Legacy `official-vps` vertical slice (regression)

```sh
uv run mcrctl init ./deployment --profile official-vps
uv run mcrctl validate --project ./deployment
uv run mcrctl repo check --project ./deployment
uv run mcrctl plan --project ./deployment
uv run mcrctl accept-eula --project ./deployment --yes
uv run mcrctl render --project ./deployment --output ./deployment/generated
```

`plan` stops until EULA acceptance and immutable artifact identities are present. This includes the homepage version / archive SHA-256 as well as OCI images, Paper, and plugin JARs. It never converts an unresolved selector into a production deployment implicitly. `render` writes Compose, Caddy, Scratch runtime, Bridge route, and ServerBackup configuration only after the same gates pass. This legacy path is currently a deterministic plan/render regression fixture, not the first home live deployment. Applying generated files to a host is not implemented. The initialized lock is intentionally version-neutral: a profile selects topology and policy, not a Minecraft or McRemote release. Existing-server migration can therefore pin the recovered artifacts without being forced to upgrade McRemote as part of the infrastructure move.

### Optional beta instance on the same VPS

The `official-vps` preset includes an optional `beta` instance. Setting `beta.enabled: true` renders a `minecraft-beta` service with independent data, backup, OCI image, Paper, and plugin locks. Stable and beta both use the standard `25565/tcp+udp` and `25575/tcp` ports and therefore run exclusively. The stable public names are unsuffixed (`scratch.mc-remote.com`, `bridge.mc-remote.com`, and `sb.mc-remote.com`); beta uses the `-beta` suffix.

`minecraft-stable` and `minecraft-beta` belong to separate Compose profiles, so an ordinary `docker compose up` starts neither Minecraft channel. On a 6 GB VPS, do not run stable and beta together. Use the generated exclusive switch operations, which announce the change, run `save-all flush`, stop gracefully, check the standard ports, and restore the previous instance on failure:

```sh
sudo bash /etc/mc-remote/generated/operations/use-beta.sh
sudo bash /etc/mc-remote/generated/operations/use-stable.sh
```

Only a stopped instance counts as dormant. Before removing the exclusive switch and running both instances continuously, test both workloads together and inspect their heaps, host memory, swap, tick time, and disk I/O.

## Encrypted off-host backup transfer

The initial transfer adapter encrypts a ServerBackup archive with a public age recipient before opening an explicit FTPS session. It requires certificate verification, protects the data connection, uses passive mode, uploads through a temporary remote name, and verifies the final remote size. `--verify-download` additionally downloads the remote ciphertext and compares its SHA-256. Plaintext and encrypted local files remain in the queue; transfer does not prune them.

```sh
uv run mcrctl backup transfer /backup/outbox/backup.zip \
  --project ./deployment \
  --verify-download
```

The FTPS password is referenced as `secret://backup_ftps_password` and stored with `mcrctl secret set`; it is never placed in the deployment project. A VPS-only user can instead download and upload outbox artifacts over the existing SSH/SFTP path. The package does not install an FTP daemon on the VPS. A snapshot that exists only on the VPS is local recovery state, not an off-host backup.

Inspect an existing whole-server recovery point without extracting its secret-bearing contents:

```sh
uv run mcrctl archive inspect /path/to/backup.zip --json
```

The result contains the archive SHA-256, ZIP CRC result, aggregate sizes, region count, root server JAR identities, and active `plugins/*.jar` SHA-256 values. Nested Paper remap caches and plugin libraries are counted but not misreported as active plugins. It does not print plugin configuration contents.

Import only the Paper and plugin JAR members named by a deployment lock from a recovery archive:

```sh
uv run mcrctl artifact import-archive /path/to/backup.zip --project ./deployment
```

The command verifies the whole archive SHA-256, requires each named member to exist exactly once, verifies each artifact SHA-256 while streaming, and writes only those JARs to a content-addressed local store. It does not extract world data or plugin configuration. `MC_REMOTE_ARTIFACT_HOME` can relocate the local store; `--store` selects an explicit SHA-256 store directory.

Rendered Minecraft Compose configuration mounts the locked Paper JAR through `PAPER_CUSTOM_JAR` and mounts each locked plugin read-only through the image's `/plugins` attach point. Startup removes only top-level old JARs from `/data/plugins` before synchronizing the locked set, so plugin data directories remain while stale renamed JARs are removed. Runtime download of Paper defaults is disabled; generated configuration is synchronized from `/config`.

The official profile also renders `mc-remote.com` and `www.mc-remote.com` as a Caddy static site. Homepage content is mounted read-only from `/var/lib/mc-remote/homepage/sha256/<sha256>`, while certificates, private keys, and ACME state remain in Caddy's separate `/data` volume. A recovered rental-server archive may use `source_archive` provenance to record its source SHA-256, source root, and intentionally excluded host-specific files.
