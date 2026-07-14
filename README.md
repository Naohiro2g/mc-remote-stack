# mc-remote-stack

[日本語はこちら。](README_ja.md)

`mc-remote-stack` is the reproducible deployment and operations package for McRemote servers. It turns one human-edited `mc-remote.yml` into validated, digest-pinned runtime configuration.

The project is intentionally separate from:

- `mc-remote-knowledge`: architecture and decision SSOT.
- `server-runbook`: fresh-host bootstrap and operational knowledge.
- a deployment project: instance-specific desired state and lock data.

## Development

```sh
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run mcrctl --help
```

## First vertical slice

```sh
uv run mcrctl init ./deployment --profile official-vps
uv run mcrctl validate --project ./deployment
uv run mcrctl repo check --project ./deployment
uv run mcrctl plan --project ./deployment
uv run mcrctl accept-eula --project ./deployment --yes
uv run mcrctl render --project ./deployment --output ./deployment/generated
```

`plan` stops until EULA acceptance and immutable artifact identities are present. This includes the homepage version / archive SHA-256 as well as OCI images, Paper, and plugin JARs. It never converts an unresolved selector into a production deployment implicitly. `render` writes Compose, Caddy, Scratch runtime, Bridge route, and ServerBackup configuration only after the same gates pass. Applying those files to a host is not implemented in this first vertical slice. The initialized lock is intentionally version-neutral: a profile selects topology and policy, not a Minecraft or McRemote release. Existing-server migration can therefore pin the recovered artifacts without being forced to upgrade McRemote as part of the infrastructure move.

### Optional staging instance on the same VPS

The `official-vps` preset includes an optional `staging` instance. Setting `staging.enabled: true` renders a `minecraft-dev` service with independent data, backup, OCI image, Paper, and plugin locks. Production publishes `25565/tcp+udp` and `25575/tcp`; staging publishes `25566/tcp+udp` and `25576/tcp`. Scratch stable defaults to `sb.mc-remote.com`, while Scratch dev defaults to `sb-dev.mc-remote.com`.

`minecraft-dev` belongs to the Compose `staging` profile, so an ordinary `docker compose up` does not start it. Start it explicitly when needed:

```sh
sudo docker compose --profile staging up -d minecraft-dev
```

Only a stopped instance counts as dormant. Before running both instances continuously, test both workloads together and inspect their heaps, host memory, swap, tick time, and disk I/O.

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
