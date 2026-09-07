# Release artifact／preset準備runbook

このrunbookは収集経路（release tagから一組のimmutable presetを作るところまで）を扱う。ケータリング
（`mc-remote.toml`への記入）とデプロイ（`mcrctl deployment update`実行）は`public-vps-bootstrap-guide_ja.md`
が扱う。完成したexact preset refをそちらへ渡す。

## 1. release tagを一組にする

Stack担当が受け取る正式な入力は、確定済みrelease gateが示す各componentのexact release tagだけである。
ユーザーの「b7」のようなrelease nameは、release gateが示すexact tagへ対応させる。tag以外の値（commit、
digest、schema内容）を会話やhandoffテキストから個別に受け取らず、必ずmanifestが示すidentityまたはprovider
APIから取得した実物のidentityを使う。

| 入力 | 内容 |
| --- | --- |
| release tag | 各component（McRemote／Python client／Scratch editor）のexact release tag |
| topology | 対象deploymentが必要とするcomponent role集合 |
| carried foundation | Paper／Minecraft runtime等、今回も使う検証済みthird-party identity |

release gateが未確定なら収集を進めない。作業は最新のreview済みStack commitから作った専用branchの
repository rootで行う。

```sh
MC_REMOTE_STACK="<Stack checkout>"
cd "$MC_REMOTE_STACK"
test "$(git rev-parse --show-toplevel)" = "$MC_REMOTE_STACK"
```

## 2. manifest.jsonを持つcomponent（Scratch）をReleaseから一括収集する

対象repoのReleaseへ`manifest.json`が添付されていれば、それが正式入力である。手作業でcommitやOCI digestを
推論・転記しない。Scratch／BridgeのOCI imageはGHCR（`ghcr.io/naohiro2g/mc-remote-scratch`等）にあり、
manifestが直接`locator`と`digest`を示す。

下のtagはmanifest.json導入時の最初のpost-release例であり、ハイフン区切り（`-post1`）のまま公開済みのため
差し替えない。post2以降のpost-release接尾辞はPEP 440の正準形に合わせ`.postN`（ドット区切り、例
`v2301.0.0b7.post2`）を使う。この例のハイフンをそのまま次のpost releaseへ転用しない
（`2026-09-07-03`、`10-protocol/versioning-design_ja.md`§10.5）。

```sh
SCRATCH_TAG="v2301.0.0b7-post1"
SCRATCH_REVIEW_DIR="$(mktemp -d)"

gh release download "$SCRATCH_TAG" \
  --repo Naohiro2g/scratch-editor \
  --pattern "manifest.json" \
  --pattern "contracts.tar.gz" \
  --dir "$SCRATCH_REVIEW_DIR"
uv run mcrctl release-manifest verify "$SCRATCH_REVIEW_DIR/manifest.json"
```

`RELEASE-MANIFEST`行のtagとsource commit、各`ARTIFACT`行のrole／kind／`locator`＋`digest`（`scratch`／`bridge`）
または`file`＋`sha256`（`contracts`／`wirescope`／`wirescope-manifest`）を収集表へ転記する。OCI artifactは
manifestが示すidentity（`locator`と`digest`）をそのままpreset artifactへ固定し、実在性はStack側の
`artifact fetch`（digest指定pull）が確認する。手動のGit tree比較や、commitからdigestを探し当てる推論は
行わない。

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

## 3. manifest.jsonが未対応のcomponent（McRemote・Python client）をGitHub Releasesで照合する

まだ`manifest.json`を添付していないcomponentは、GitHub APIが返すasset単位のSHA-256をそのまま期待値にする。
このsectionはcomponentがmanifest.jsonを添付し次第、§2の手順へ統合し削除する（`2026-09-06-02`）。

```sh
MC_REMOTE_TAG="v1.21.11-2301.0.0b7"
MC_REMOTE_ASSET="mc-remote-1.21.11-2301.0.0b7.jar"

gh api "repos/Naohiro2g/McRemote/releases/tags/$MC_REMOTE_TAG" \
  --jq '{tag_name,target_commitish,draft,prerelease,assets:[.assets[]|{name,browser_download_url,digest}]}'
MC_REMOTE_PROVIDER_DIGEST="$(gh api \
  "repos/Naohiro2g/McRemote/releases/tags/$MC_REMOTE_TAG" \
  --jq ".assets[] | select(.name == \"$MC_REMOTE_ASSET\") | .digest")"
MC_REMOTE_EXPECTED_SHA256="${MC_REMOTE_PROVIDER_DIGEST#sha256:}"

ARTIFACT_REVIEW_DIR="$(mktemp -d)"
gh release download "$MC_REMOTE_TAG" \
  --repo Naohiro2g/McRemote \
  --pattern "$MC_REMOTE_ASSET" \
  --dir "$ARTIFACT_REVIEW_DIR"
test "$(sha256sum "$ARTIFACT_REVIEW_DIR/$MC_REMOTE_ASSET" | awk '{print $1}')" = \
  "$MC_REMOTE_EXPECTED_SHA256"
```

presetには、このtagのasset URL、filename、version、確認したSHA-256を記録する。

## 4. foundation artifactを公式配布元で照合する

topologyが使うPaperは公式Paper配布URLから取得し、McRemote assetと同じく`sha256sum`を期待値へ一致させる。
Minecraft runtime等のOCI imageは、その公式OCI registryを`docker buildx imagetools inspect
<image>@sha256:<digest>`で照合する。今回変更しないfoundationも、採用元presetのartifact identityと今回の
topology要件が一致することを確認して収集表へ載せる。

収集表は次の列を一組にする。

```text
role | kind | version/tag | source commit | official locator | exact digest | verification
```

## 5. git-build artifactを例外経路として固定する（新規presetでは選ばない）

component担当がまだRelease asset化していないbuild成果物を返す場合だけ、repository、full commit、recipe、
toolchain、build input、output SHA-256を一組で受け取り、reviewed outputを期待SHA-256へ一致させてCASへ収容
する。

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

presetには、必要な全component role、各artifactの実際の`locator`／`digest`または`filename`／`sha256`
／`origin`、Scratchの`source_commit`を収集表から転記する。`src/mc_remote_stack/data/preset_catalog_policy.toml`
へlifecycleを設定し、catalogを正準commandで再生成する。

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

Scratch contract testは収容tree、schema／fixture digest、accept／reject判定、Scratch image digestとの一致を
確認する。renderer testは全artifactがdigest固定され、Scratch runtime configとBridge allowlistが同じtarget
集合から生成されることを確認する。`apply` testはhost port preflight後にHTTPS artifactをSHA-256付きCASへ
取得し、OCI imageをdigest参照でpullすることを確認する。

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

deployment担当はこのexact preset refを`mc-remote.toml`へ設定する。通常operator経路の`apply`はlockが指す
artifactを使用し、OCI pull、render、create／update判定、起動までを行う。`doctor`がlive identityを確認する。
