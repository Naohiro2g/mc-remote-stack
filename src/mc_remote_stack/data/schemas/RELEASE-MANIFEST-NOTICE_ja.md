# Release manifest v2 契約の出典

`release-manifest-v2.schema.json`と`fixtures/release-manifest-v2.json`は
Naohiro2g/minecraft-remote-toolingの固定commit
`fb6880b192a0063241f95c44a5fa6b836f5e7394`から無変更で収容しています。
source path、bytes、SHA-256は`../release-manifest-lock.json`に記録しています。
原本のライセンス本文は`TOOLING-LICENSE`です。Stackのv1 schemaとPython実装とは分けています。

正本はknowledge `6dbb9f1ee192c6c46d8dd58fdd91f8e6c3f46de5`の
`00-hub/release-gate-notes_ja.md` b10「release manifest v2の形」（DEC `2026-10-07-09`）。
更新はtooling担当が発行した固定commitとschema・fixture一組で行います。
本番でmainやremoteを取得せず、別のschemaへfallbackしません。
