# Release artifact／preset準備runbook

このrunbookは収集経路（release tagから一組のimmutable presetを作るところまで）を扱う。ケータリング（`mc-remote.toml`への記入）とデプロイ（`mcrctl deployment update`実行）は`public-vps-bootstrap-guide_ja.md`が扱う。完成したexact preset refをそちらへ渡す。

v2のmanifestでは、配布物を取得する前に[Release manifest v2の収集・照合](release-manifest-v2-guide_ja.md)を使います。
`select`でrole・期待kind・明示OS／archを確定し、`collect`でbytesとSHA、Minecraftの宣言・全record・preset構成を照合します。
以下の収集例は既存v1向けです。v2で構成がverificationと違う場合はcoordinatorへ返します。

## 1. release tagを一組にする

Stack担当が受け取る正式な入力は、確定済みrelease gateが示す各componentのexact release tagだけである。ユーザーの「b7」のようなrelease nameは、release gateが示すexact tagへ対応させる。tag以外の値（commit、digest、schema内容）を会話やhandoffテキストから個別に受け取らず、必ずmanifestが示すidentityまたはprovider APIから取得した実物のidentityを使う。

| 入力 | 内容 |
| --- | --- |
| release tag | 各component（McRemote／Python client／Scratch editor）のexact release tag |
| topology | 対象deploymentが必要とするcomponent role集合 |
| carried foundation | Paper／Minecraft runtime等、今回も使う検証済みthird-party identity |

release gateが未確定なら収集を進めない。作業は最新のreview済みStack commitから作った専用branchのrepository rootで行う。

```sh
MC_REMOTE_STACK="<Stack checkout>"
cd "$MC_REMOTE_STACK"
test "$(git rev-parse --show-toplevel)" = "$MC_REMOTE_STACK"
```

今回収集する組合せは、対象リリースのrelease gateが示すexact tagを使います。コメントには、記入する値がわかるよう過去の公開値を例示しています。`MC_REMOTE_ASSET`には、対象tagのmanifestが示すJARの`file`名を記入します。

```sh
SCRATCH_TAG="<今回のexact tag>"       # 過去の公開値の例：v2301.0.0b7-post1
MC_REMOTE_TAG="<今回のexact tag>"     # 過去の公開値の例：v1.21.11-2320.0.0b8
MC_REMOTE_ASSET="<manifestのfile名>"  # 過去の公開値の例：mc-remote-1.21.11-2320.0.0b8.jar
PYTHON_TAG="<今回のexact tag>"        # 過去の公開値の例：v2320.0.0b8
```

以降のコマンドは、この変数を設定した同じシェルで実行します。tagの表記方針は[versioning設計 §10.5](https://github.com/Naohiro2g/mc-remote-knowledge/blob/main/10-protocol/versioning-design_ja.md#105-接尾辞と表記)を参照してください。

## 2. manifest.jsonを持つcomponentをReleaseから一括収集する

対象repoのReleaseへ`manifest.json`が添付されていれば、それが正式入力である。手作業でcommitやOCI digestを推論・転記しない。Scratch／BridgeのOCI imageはGHCR（`ghcr.io/naohiro2g/mc-remote-scratch`等）にあり、manifestが直接`locator`と`digest`を示す。

```sh
SCRATCH_REVIEW_DIR="$(mktemp -d)"

gh release download "$SCRATCH_TAG" \
  --repo Naohiro2g/scratch-editor \
  --pattern "manifest.json" \
  --pattern "contracts.tar.gz" \
  --dir "$SCRATCH_REVIEW_DIR"
uv run mcrctl release-manifest verify "$SCRATCH_REVIEW_DIR/manifest.json"
```

`RELEASE-MANIFEST`行のtagとsource commit、各`ARTIFACT`行のrole／kind／`locator`＋`digest`（`scratch`／`bridge`）または`file`＋`sha256`（`contracts`／`wirescope`／`wirescope-manifest`）を収集表へ転記する。OCI artifactはmanifestが示すidentity（`locator`と`digest`）をそのままpreset artifactへ固定し、実在性はStack側の`artifact fetch`（digest指定pull）が確認する。手動のGit tree比較や、commitからdigestを探し当てる推論は行わない。

```sh
SCRATCH_CONTRACT_SHA256="$(python3 -c '
import json, sys
manifest = json.load(open(sys.argv[1]))
print(next(a["sha256"] for a in manifest["artifacts"] if a["role"] == "contracts"))
' "$SCRATCH_REVIEW_DIR/manifest.json")"
test "$(sha256sum "$SCRATCH_REVIEW_DIR/contracts.tar.gz" | awk '{print $1}')" = \
  "$SCRATCH_CONTRACT_SHA256"

SCRATCH_SOURCE_COMMIT="$(python3 -c '
import json, sys
print(json.load(open(sys.argv[1]))["source_commit"])
' "$SCRATCH_REVIEW_DIR/manifest.json")"
SCRATCH_CONTRACT_DEST="src/mc_remote_stack/data/scratch-contracts/$SCRATCH_SOURCE_COMMIT"
install -d "$SCRATCH_CONTRACT_DEST"
tar -xzf "$SCRATCH_REVIEW_DIR/contracts.tar.gz" -C "$SCRATCH_CONTRACT_DEST" \
  --strip-components=2 contracts/runtime-config
```

収容先は次の形になる。schema、全fixture、manifestの`source_commit`をpresetへ記録する。

```text
src/mc_remote_stack/data/scratch-contracts/<SCRATCH_SOURCE_COMMIT>/
```

## 3. McRemote・Python clientの配布物を照合する

McRemoteとPython clientもGitHub Releasesの`manifest.json`を検査し、tag、source commit、artifactのfile／SHA-256を配布物へ照合します。GitHub APIのasset digestも照合できます。manifestがない以前のreleaseを扱う場合は、このAPIのSHA-256を期待値にします。

```sh
gh api "repos/Naohiro2g/McRemote/releases/tags/$MC_REMOTE_TAG" \
  --jq '{tag_name,target_commitish,draft,prerelease,assets:[.assets[]|{name,browser_download_url,digest}]}'
MC_REMOTE_PROVIDER_DIGEST="$(gh api \
  "repos/Naohiro2g/McRemote/releases/tags/$MC_REMOTE_TAG" \
  --jq ".assets[] | select(.name == \"$MC_REMOTE_ASSET\") | .digest")"
MC_REMOTE_EXPECTED_SHA256="${MC_REMOTE_PROVIDER_DIGEST#sha256:}"

ARTIFACT_REVIEW_DIR="$(mktemp -d)"
gh release download "$MC_REMOTE_TAG" \
  --repo Naohiro2g/McRemote \
  --pattern "manifest.json" \
  --pattern "$MC_REMOTE_ASSET" \
  --dir "$ARTIFACT_REVIEW_DIR"
uv run mcrctl release-manifest verify "$ARTIFACT_REVIEW_DIR/manifest.json"
MC_REMOTE_MANIFEST_SHA256="$(python3 -c '
import json, sys
manifest = json.load(open(sys.argv[1]))
print(next(a["sha256"] for a in manifest["artifacts"] if a["role"] == "jar"))
' "$ARTIFACT_REVIEW_DIR/manifest.json")"
test "$MC_REMOTE_MANIFEST_SHA256" = "$MC_REMOTE_EXPECTED_SHA256"
test "$(sha256sum "$ARTIFACT_REVIEW_DIR/$MC_REMOTE_ASSET" | awk '{print $1}')" = \
  "$MC_REMOTE_EXPECTED_SHA256"
```

presetには、このtagのasset URL、filename、version、確認したSHA-256を記録します。Python clientは、冒頭で設定した`PYTHON_TAG`のReleaseからmanifest、wheel、sdistを取得します。

```sh
PYTHON_REVIEW_DIR="$(mktemp -d)"
gh release download "$PYTHON_TAG" \
  --repo Naohiro2g/minecraft-remote-api \
  --pattern "manifest.json" \
  --pattern "*.whl" \
  --pattern "*.tar.gz" \
  --dir "$PYTHON_REVIEW_DIR"
uv run mcrctl release-manifest verify "$PYTHON_REVIEW_DIR/manifest.json"
sha256sum "$PYTHON_REVIEW_DIR"/*.whl "$PYTHON_REVIEW_DIR"/*.tar.gz
```

`wheel`／`sdist`の各`ARTIFACT`行のfile／SHA-256を、ダウンロードした実物の名前と`sha256sum`の出力へ照合します。以前のmanifestがないreleaseでは、downloadのmanifest指定・verify・manifestとの比較を省き、APIと実物を照合します。

## 4. foundation artifactを公式配布元で照合する

topologyが使うPaperは公式Paper配布URLから取得し、McRemote assetと同じく`sha256sum`を期待値へ一致させる。Minecraft runtime等のOCI imageは、その公式OCI registryを`docker buildx imagetools inspect<image>@sha256:<digest>`で照合する。今回変更しないfoundationも、採用元presetのartifact identityと今回のtopology要件が一致することを確認して収集表へ載せる。

収集表は次の列を一組にする。

```text
role | kind | os/arch | bytes | version/tag | source commit | official locator | exact digest | compatibility/verification
```

## 5. git-build artifactを例外経路として固定する（新規presetでは選ばない）

component担当がまだRelease asset化していないbuild成果物を返す場合だけ、repository、full commit、recipe、toolchain、build input、output SHA-256を一組で受け取り、reviewed outputを期待SHA-256へ一致させてCASへ収容する。

```sh
GIT_BUILD_PROJECT="<resolved deployment project>"
GIT_BUILD_ARTIFACT_ID="<lockのartifact id>"
REVIEWED_OUTPUT="<reviewed output path>"
REVIEWED_OUTPUT_SHA256="<component担当が返したoutput SHA-256>"

test "$(sha256sum "$REVIEWED_OUTPUT" | awk '{print $1}')" = "$REVIEWED_OUTPUT_SHA256"
uv run mcrctl artifact import-reviewed "$REVIEWED_OUTPUT" \
  --project "$GIT_BUILD_PROJECT" \
  --artifact-id "$GIT_BUILD_ARTIFACT_ID" \
  --expected-sha256 "$REVIEWED_OUTPUT_SHA256"
```

新規presetはこの経路を選ばない。component担当がRelease asset化するまでの一時的な例外として扱う。

## 6. append-only presetを作る

新しいrevisionを次へ追加する。

```text
src/mc_remote_stack/data/preset_registry/<name>/<revision>/preset.toml
```

presetには、必要な全component role、各artifactの実際の`locator`／`digest`または`filename`／`sha256`／`origin`、Scratchの`source_commit`を収集表から転記する。`src/mc_remote_stack/data/preset_catalog_policy.toml`へlifecycleを設定し、catalogを正準commandで再生成する。

```sh
PRESET_REF="<name>@<revision>"
uv run tools/rebuild-preset-catalog.py
uv run tools/rebuild-preset-catalog.py --check
uv run mcrctl preset show "$PRESET_REF"
```

表示されたpreset ref、semantic digest、component／artifact一覧を収集表と照合する。

## 7. presetを決定論的に検証する

```sh
uv sync --extra dev
uv run pytest
uv run ruff check .
git diff --check
```

Scratch contract testは収容tree、schema／fixture digest、accept／reject判定、Scratch image digestとの一致を確認する。renderer testは全artifactがdigest固定され、Scratch runtime configとBridge allowlistが同じtarget集合から生成されることを確認する。`apply` testはhost port preflight後にHTTPS artifactをSHA-256付きCASへ取得し、OCI imageをdigest参照でpullすることを確認する。

## 8. deployment handoffへ渡す

準備完了時は次を一組で返す。

```text
release name: <指定release>
preset ref: <name>@<revision>
preset semantic digest: <sha256>
component release tags: <McRemote／Python client／Scratchの各tag>
manifest verification: <mcrctl release-manifest verifyの結果>
artifact verification: PASS
tests: <実行commandとPASS>
stack commit: <push済みcommit>
```

deployment担当はこのexact preset refを`mc-remote.toml`へ設定する。通常operator経路の`apply`はlockが指すartifactを使用し、OCI pull、render、create／update判定、起動までを行う。`doctor`がlive identityを確認する。
