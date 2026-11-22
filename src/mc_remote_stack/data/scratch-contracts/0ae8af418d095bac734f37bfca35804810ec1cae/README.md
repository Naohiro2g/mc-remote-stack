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

## v2：接続先IDと独立したhost・port（案）

v1の`{id, label, sandbox}`は従来どおり読みます。v2は`default_sandbox`を`default_target_id`へ、
`connection_targets`を`[{id, label, host, port}, ...]`へ変更します。新旧フィールドは混在させません。
ブラウザは`?sandbox=<id>`で接続し、host・portは一覧の情報として扱います。ID対応Bridgeが必要です。
IDの重複や一覧外の既定は拒否します。詳細は`mc-remote/local/CONNECTION_TARGETS_PROPOSAL_ja.md`にあります。
