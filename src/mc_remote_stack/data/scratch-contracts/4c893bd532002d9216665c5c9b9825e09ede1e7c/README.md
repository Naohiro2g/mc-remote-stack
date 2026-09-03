# McRemote runtime configuration contract

`schema.json` and `fixtures/` are the machine-readable handoff from Scratch to deployment tooling.
Deployment tooling owns the complete runtime file and mounts it read-only at:

```text
/usr/share/nginx/html/mc-remote-runtime-config.json
```

The image-owned `/usr/share/nginx/html/mc-remote-product-config.json` is a separate file and must not be generated,
mounted, or overwritten by deployment tooling. A disabled runtime needs only `schema_version` and
`connection_enabled`. `storage_persist_enabled` is an optional compatibility carry from the existing Scratch browser
storage feature; deployment tooling may omit it.

Unknown fields are rejected at every object level. Consumers should validate against `schema.json` without deriving
additional fields from Scratch source code.
