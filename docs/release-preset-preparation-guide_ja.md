# Release artifact／preset準備runbook

このrunbookは、指定されたMcRemote release nameを、Stackが取得可能な一組のimmutable presetへ
変換する正準手順である。component担当がbuild／publishしたartifact、またはcomponent handoffが明示する
git-build artifactのreviewed bytesを、Stack担当がexact identityへ照合してpresetへ固定する。
完成したexact preset refをdeployment runbookへ渡す。

## 1. release handoffを一組にする

Stack担当は次の入力を受け取る。ユーザーの「b7」のようなrelease nameは、確定済みrelease gateまたは
component release handoffが示すexact tag／commitへ対応させる。

| 入力 | 内容 |
| --- | --- |
| release name | ユーザーが指定したb6／b7等のrelease |
| component release handoff | McRemoteのexact tag、asset名、source commit |
| Scratch contract handoff | contract commit／directory tree／schema／fixtures、Scratch・Bridge OCI digest |
| topology | 対象deploymentが必要とするcomponent role集合 |
| carried foundation | Paper／Minecraft runtime等、今回も使う検証済みthird-party identity |

release gateが未確定なら、収集結果をcandidate presetとしてreviewへ戻す。確定済みreleaseなら、そのreleaseの
exact identityだけを使う。preset作成中に別releaseへ選択を広げない。

作業は最新のreview済みStack commitから作った専用branchのrepository rootで行う。

```sh
MC_REMOTE_STACK="<Stack checkout>"
cd "$MC_REMOTE_STACK"
test "$(git rev-parse --show-toplevel)" = "$MC_REMOTE_STACK"
```

## 2. McRemote release assetをGitHub Releasesで照合する

handoff値を設定し、tag、target commit、asset URL、provider側digestを表示する。

```sh
MC_REMOTE_TAG="v1.21.11-2301.0.0b7"
MC_REMOTE_ASSET="mc-remote-1.21.11-2301.0.0b7.jar"
MC_REMOTE_SOURCE_COMMIT="<handoffのfull commit>"

gh api "repos/Naohiro2g/McRemote/releases/tags/$MC_REMOTE_TAG" \
  --jq '{tag_name,target_commitish,draft,prerelease,assets:[.assets[]|{name,browser_download_url,digest}]}'
test "$(gh api "repos/Naohiro2g/McRemote/commits/$MC_REMOTE_TAG" --jq .sha)" = \
  "$MC_REMOTE_SOURCE_COMMIT"
MC_REMOTE_PROVIDER_DIGEST="$(gh api \
  "repos/Naohiro2g/McRemote/releases/tags/$MC_REMOTE_TAG" \
  --jq ".assets[] | select(.name == \"$MC_REMOTE_ASSET\") | .digest")"
MC_REMOTE_EXPECTED_SHA256="${MC_REMOTE_PROVIDER_DIGEST#sha256:}"
test "sha256:$MC_REMOTE_EXPECTED_SHA256" = "$MC_REMOTE_PROVIDER_DIGEST"
```

asset本体を一時review directoryへ取得し、GitHub APIが返したSHA-256と一致させる。

```sh
ARTIFACT_REVIEW_DIR="$(mktemp -d)"
gh release download "$MC_REMOTE_TAG" \
  --repo Naohiro2g/McRemote \
  --pattern "$MC_REMOTE_ASSET" \
  --dir "$ARTIFACT_REVIEW_DIR"
test "$(sha256sum "$ARTIFACT_REVIEW_DIR/$MC_REMOTE_ASSET" | awk '{print $1}')" = \
  "$MC_REMOTE_EXPECTED_SHA256"
```

presetには、このtagのasset URL、filename、version、確認したSHA-256を記録する。

## 3. Scratch contractとGHCR imageを照合する

Scratch contract handoffのcommitがGitHubに存在し、handoffのdirectory tree SHAがそのcommit内の
runtime-config contractを指すことを確認する。内容として取得するのはcontract directoryだけである。

```sh
SCRATCH_COMMIT="<handoffのfull commit>"
SCRATCH_CONTRACT_TREE="<handoffのdirectory tree SHA>"
SCRATCH_CONTRACT_PATH="packages/scratch-gui/contracts/runtime-config"

test "$(gh api "repos/Naohiro2g/scratch-editor/commits/$SCRATCH_COMMIT" --jq .sha)" = \
  "$SCRATCH_COMMIT"
SCRATCH_ROOT_TREE="$(gh api "repos/Naohiro2g/scratch-editor/git/commits/$SCRATCH_COMMIT" \
  --jq .tree.sha)"
test "$(gh api "repos/Naohiro2g/scratch-editor/git/trees/$SCRATCH_ROOT_TREE?recursive=1" \
  --jq ".tree[] | select(.path == \"$SCRATCH_CONTRACT_PATH\") | .sha")" = \
  "$SCRATCH_CONTRACT_TREE"
```

収容先に同じcommitがあればそのbytesをtest対象にする。未収容ならexact commitからcontract directoryだけを
取得し、Stackのhandoff収容先へ配置する。

```sh
SCRATCH_SOURCE="$(mktemp -d)"
SCRATCH_CONTRACT_STAGE="$(mktemp -d)"
SCRATCH_CONTRACT_DEST="src/mc_remote_stack/data/scratch-contracts/$SCRATCH_COMMIT"

git clone --filter=blob:none --no-checkout \
  https://github.com/Naohiro2g/scratch-editor.git "$SCRATCH_SOURCE"
git -C "$SCRATCH_SOURCE" fetch --depth=1 origin "$SCRATCH_COMMIT"
git -C "$SCRATCH_SOURCE" archive "$SCRATCH_COMMIT" "$SCRATCH_CONTRACT_PATH" \
  | tar -x -C "$SCRATCH_CONTRACT_STAGE"
install -d "$SCRATCH_CONTRACT_DEST"
cp -a "$SCRATCH_CONTRACT_STAGE/$SCRATCH_CONTRACT_PATH/." "$SCRATCH_CONTRACT_DEST/"
```

収容先は次の形になる。schema、全fixture、Git tree identityをpresetへ記録する。

```text
src/mc_remote_stack/data/scratch-contracts/<SCRATCH_COMMIT>/
```

ScratchとBridgeはhandoffのtagとOCI index digestをGHCRで照合する。

```sh
SCRATCH_IMAGE="ghcr.io/naohiro2g/mc-remote-scratch"
SCRATCH_EXPECTED_DIGEST="sha256:<handoff digest>"
BRIDGE_IMAGE="ghcr.io/naohiro2g/mc-remote-bridge"
BRIDGE_EXPECTED_DIGEST="sha256:<handoff digest>"

test "$(docker buildx imagetools inspect "$SCRATCH_IMAGE:sha-$SCRATCH_COMMIT" \
  | awk '$1=="Digest:" {print $2; exit}')" = "$SCRATCH_EXPECTED_DIGEST"
docker buildx imagetools inspect "$SCRATCH_IMAGE@$SCRATCH_EXPECTED_DIGEST"
test "$(docker buildx imagetools inspect "$BRIDGE_IMAGE:sha-$SCRATCH_COMMIT" \
  | awk '$1=="Digest:" {print $2; exit}')" = "$BRIDGE_EXPECTED_DIGEST"
docker buildx imagetools inspect "$BRIDGE_IMAGE@$BRIDGE_EXPECTED_DIGEST"
```

各tagの`Digest`をhandoff値と一致させ、presetのOCI artifactは`locator`と`digest`へ分けて固定する。

## 4. git-build artifactをreviewed bytesへ固定する

component handoffがdistribution modeとして`git-build`を指定した場合は、repository、full commit、recipe、toolchain、
build input、output SHA-256を一組で受け取る。component担当が渡したreviewed output、またはhandoffが再現を委任した
場合にartifact準備環境でexact recipeから作ったoutputを、期待SHA-256へ一致させる。

resolved lockが指すartifact idと同じ一件をCASへ収容する。

```sh
GIT_BUILD_PROJECT="<resolved deployment project>"
GIT_BUILD_ARTIFACT_ID="<lockのartifact id>"
REVIEWED_OUTPUT="<reviewed output path>"
REVIEWED_OUTPUT_SHA256="<handoffのoutput SHA-256>"

test "$(sha256sum "$REVIEWED_OUTPUT" | awk '{print $1}')" = "$REVIEWED_OUTPUT_SHA256"
uv run mcrctl artifact import-reviewed "$REVIEWED_OUTPUT" \
  --project "$GIT_BUILD_PROJECT" \
  --artifact-id "$GIT_BUILD_ARTIFACT_ID" \
  --expected-sha256 "$REVIEWED_OUTPUT_SHA256"
```

OCI imageにはこの経路を使わない。OCIはcomponent ownerのCIがpublishしたtag／digestを§3で照合する。
artifact準備環境とdeployment hostを分け、通常`apply`はCASのreviewed bytesを使用する。

## 5. foundation artifactを公式配布元で照合する

topologyが使うPaperは公式Paper配布URLから取得し、McRemote assetと同じく`sha256sum`を期待値へ一致させる。
Minecraft runtime等のOCI imageは、その公式OCI registryを`docker buildx imagetools inspect
<image>@sha256:<digest>`で照合する。今回変更しないfoundationも、採用元presetのartifact identityと
今回のtopology要件が一致することを確認して収集表へ載せる。

収集表は次の列を一組にする。

```text
role | kind | version/tag | source commit | official locator | exact digest | verification
```

## 6. append-only presetを作る

新しいrevisionを次へ追加する。

```text
src/mc_remote_stack/data/preset_registry/<name>/<revision>/preset.toml
```

presetには、必要な全component role、各artifactの公式locatorとexact digest、Scratch contract identityを
収集表から転記する。`src/mc_remote_stack/data/preset_catalog_policy.toml`へlifecycleを設定し、catalogを
正準commandで再生成する。

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
component release handoff: <参照identity>
Scratch contract handoff: <commit/tree/schema/fixture/image digest>
artifact verification: PASS
tests: <実行commandとPASS>
stack commit: <push済みcommit>
```

deployment担当はこのexact preset refを`mc-remote.toml`へ設定する。通常operator経路の`apply`は
component担当がpublishしたexact artifactだけを取得し、OCI pull、lock、render、create／update判定、起動までを
行う。`doctor`がlive identityを確認する。
