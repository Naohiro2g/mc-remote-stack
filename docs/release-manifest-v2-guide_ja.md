# Release manifest v2 の収集・照合

[Release artifact／preset準備](release-preset-preparation-guide_ja.md)の収集段階で使います。
coordinatorが示したexact tagと必要roleを使い、manifest全体を検査してから配布物を選びます。
名前やtagからMinecraft版、OS、archを推定しません。既存presetのrevisionは変更しません。

## 契約の固定

v1は既存の`data/schemas/release-manifest.schema.json`で検査します。
v2はtoolingの`fb6880b192a0063241f95c44a5fa6b836f5e7394`から無変更で収容した
schemaと66件の共有fixtureを使います。`src/mc_remote_stack/data/release-manifest-lock.json`は
固定commit、source path、生bytes、SHA-256を持ち、Bridge／WireScopeのlockとは独立しています。
実行時にremoteを取得せず、schemaの欠落・改変や未知version・fieldを拒否します。
更新時はtooling担当の発行票でschemaとfixture一組を固定し、共有試験を実行します。
`--contract-dir`を使う場合も同じ構造のlockとschemaが必要です。

正本は[Knowledgeの確定契約](https://github.com/Naohiro2g/mc-remote-knowledge/blob/6dbb9f1ee192c6c46d8dd58fdd91f8e6c3f46de5/00-hub/release-gate-notes_ja.md)
b10「release manifest v2の形」（DEC `2026-10-07-09`）です。

## Scratchの選択と実ファイルの照合

```sh
SCRATCH_REVIEW_DIR="$(mktemp -d)"
gh release download "$SCRATCH_TAG" --repo Naohiro2g/scratch-editor \
  --pattern manifest.json --dir "$SCRATCH_REVIEW_DIR"
uv run mcrctl release-manifest verify "$SCRATCH_REVIEW_DIR/manifest.json"
uv run mcrctl release-manifest select "$SCRATCH_REVIEW_DIR/manifest.json" \
  contracts --kind https-file > "$SCRATCH_REVIEW_DIR/contracts-selection.json"
CONTRACT_FILE="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["file"])' \
  "$SCRATCH_REVIEW_DIR/contracts-selection.json")"
gh release download "$SCRATCH_TAG" --repo Naohiro2g/scratch-editor \
  --pattern "$CONTRACT_FILE" --dir "$SCRATCH_REVIEW_DIR"
uv run mcrctl release-manifest collect "$SCRATCH_REVIEW_DIR/manifest.json" \
  --artifact scratch:oci --artifact bridge:oci --artifact contracts:https-file \
  --asset-dir "$SCRATCH_REVIEW_DIR"
```

`verify`は文書全体のschemaと整合性を検査し、各artifactの`bytes`／`os`／`arch`、宣言、verificationを表示します。
外部fileのdigestはこの段階では`unchecked`です。`select`は期待kindと件数を検査し、選んだ1件だけをJSONで返します。
`collect`は選択したhttps-fileのSHA-256と生bytesを照合し、収集表用の`COLLECTION`行を返します。
OCIはlocatorとdigestを収集し、実在性の確認は通常のartifact取得手順へ渡します。

標準deploymentで使わない`scratch-local`は選ばず取得しません。Local版を調べる場合は、たとえば
`select ... scratch-local --kind https-file --os linux --arch x64`または
`collect ... --artifact scratch-local:https-file:linux:x64`で明示します。
1件だけの変種でもselectorが必要です。別OS／archへfallbackせず、未使用roleも文書全体の検査対象です。
WireScopeなど必要なroleを増やす場合も、先に`select`し、取得後に対応する`--artifact`を足します。

## McRemoteの宣言・全record・preset構成の照合

先にmanifestだけを取得し、検査後にJARのfile名を選びます。

```sh
ARTIFACT_REVIEW_DIR="$(mktemp -d)"
gh release download "$MC_REMOTE_TAG" --repo Naohiro2g/McRemote \
  --pattern manifest.json --dir "$ARTIFACT_REVIEW_DIR"
uv run mcrctl release-manifest verify "$ARTIFACT_REVIEW_DIR/manifest.json"
uv run mcrctl release-manifest select "$ARTIFACT_REVIEW_DIR/manifest.json" \
  jar --kind https-file > "$ARTIFACT_REVIEW_DIR/jar-selection.json"
MC_REMOTE_ASSET="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["file"])' \
  "$ARTIFACT_REVIEW_DIR/jar-selection.json")"
gh release download "$MC_REMOTE_TAG" --repo Naohiro2g/McRemote \
  --pattern "$MC_REMOTE_ASSET" --dir "$ARTIFACT_REVIEW_DIR"
```

GitHub APIのasset digestとも照合します。Minecraft対応版は`minecraft_compatibility`から読みます。
全verificationの`record.file`を**同じtagのRelease**から取得してください。recordは`artifacts`へ足しません。
宣言fileはmanifestの`source_commit`と`declaration.path`を使ってproducer repoから取得し、
JSONの再serializeをせず生bytesを保存します。`collect`は宣言のdigest・対応集合と、全recordのdigestを照合します。
呼び出し側のreaderには宣言pathとsource commitを渡します。CLIでは取得済みfileを指定します。

収集したJARとfoundationを、新規presetのTOMLに固定した後、次を実行します。

```sh
uv run mcrctl release-manifest collect "$ARTIFACT_REVIEW_DIR/manifest.json" \
  --artifact jar:https-file --asset-dir "$ARTIFACT_REVIEW_DIR" \
  --declaration-file "$DECLARATION_FILE" --preset-file "$PRESET_FILE" \
  --paper-build "$PAPER_BUILD" --java-version "$JAVA_VERSION"
```

`PRESET_FILE`は採用する新規preset、`PAPER_BUILD`はそのPaperの実測build番号（整数）、
`JAVA_VERSION`はその構成の実測Java runtime版を文字列そのままで指定します。
Javaのmajor番号やOCI tagから補完しません。presetの`paper-server.minecraft_version`を明示し、
同じpresetが参照するserver JAR SHA・McRemote JAR SHAとともに照合します。
対応集合への包含と構成の一致が確認できれば`MINECRAFT-PRESET ... foundation=matched`を表示します。
不一致・入力不足は`release_manifest_foundation_unverified`で停止し、coordinatorへ返します。
外部digestに不一致がある場合は収集成功行を出しません。

この一致はmanifestにある観測構成との照合です。shared deployment、実機でのJava版の採取、
人間参加試験やrelease gateの判定はcoordinatorの次操作に従います。
通常のapply／doctorは、これまでどおりpresetの明示Minecraft版を使います。

## 回帰試験

```sh
uv sync --extra dev
uv run pytest tests/test_release_manifest.py tests/test_release_manifest_v2.py \
  tests/test_release_manifest_shared_contract.py tests/test_release_manifest_v2_cli.py
uv run ruff check .
```

共有試験は受入・拒否だけでなくreasonとstageを比較し、元のv1 schemaのSHAも確認します。
fixtureの宣言・Paper build・Java版・recordは合成値であり、公開candidateの検証結果を表しません。
