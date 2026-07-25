# home private alpha 検証ガイド

## 0. 目的と境界

このガイドは、home private betaを変更せず、別のproject / volume / world / loopback portで
private alphaをbootstrapする。b3 component内容の検証ではなく、alpha orderとケータリング型
deployment経路の検証である。

対象は次のexact contractに限定する。

| axis | value |
| --- | --- |
| profile | `home-server@2` |
| preset | `mcremote-paper@2` |
| channel | `alpha` |
| exposure | `isolated` |
| purpose | `integration` |
| renderer | `compose@1` |

`mcremote-paper@2`は最初の検証時点ではb2のexact artifactを再利用する。live alpha evidence取得前は
`unverified`であり、理由付きorder acknowledgementとresolve / applyそれぞれのone-shot flagを要求する。

## 1. betaとの分離

alpha用に次を新規作成し、beta projectをcopyまたはrenameしない。

- project root: `$HOME/mc-remote-deployments/home-alpha`
- deployment name: `home-alpha`
- environment identity: `home-alpha`
- runtime volume: `home-alpha-minecraft-data`
- world identity: `home-alpha-world`
- loopback ports: Java `25566`、McRemote `25576`

portは同じhost上のprivate betaと共存するために分ける。apply前にhost memory、swap、disk空き容量、
既存container、volume、portを人間がreviewする。capacity不足ならbetaを停止する判断を人間へ戻し、
agentが暗黙停止しない。

## 2. 初期化

validated checkoutのrepo rootで実行する。

```bash
MC_REMOTE_PROJECT="$HOME/mc-remote-deployments/home-alpha"

uv run mcrctl init "$MC_REMOTE_PROJECT" \
  --format toml \
  --deployment-name home-alpha \
  --profile home-server@2 \
  --environment-identity home-alpha \
  --channel alpha \
  --exposure isolated \
  --purpose integration \
  --preset mcremote-paper@2 \
  --artifact-store "$HOME/.local/share/mc-remote/artifacts" \
  --volume minecraft-data=home-alpha-minecraft-data \
  --world-identity home-alpha-world \
  --bind-address 127.0.0.1 \
  --java-port 25566 \
  --mcremote-port 25576
```

Minecraft EULAを人間が確認してから専用commandで記録する。

```bash
uv run mcrctl validate --project "$MC_REMOTE_PROJECT"
uv run mcrctl repo check --project "$MC_REMOTE_PROJECT"
uv run mcrctl accept-eula --project "$MC_REMOTE_PROJECT" --yes
```

`mc-remote.toml`の`[acknowledgements]`を人間が編集し、今回この未検証presetを使用する具体的理由を
記録する。

```toml
[acknowledgements]
allow_unverified = true
unverified_reason = "initial private alpha deployment-path evidence"
allow_eol = false
eol_reason = ""
```

## 3. resolve / plan / render

```bash
uv run mcrctl resolve \
  --project "$MC_REMOTE_PROJECT" \
  --allow-unverified
uv run mcrctl plan --project "$MC_REMOTE_PROJECT"
uv run mcrctl artifact fetch --project "$MC_REMOTE_PROJECT"
uv run mcrctl render \
  --project "$MC_REMOTE_PROJECT" \
  --output "$MC_REMOTE_PROJECT/generated"
```

unverified警告によりplanは内容を表示してstatus 1を返す。これは期待されたgateであり、警告を
抑制したり成功statusへ読み替えたりしない。人間は少なくとも次をreviewする。

- profile / preset refとdigest
- channel / exposure / purpose
- lock identity
- exact artifact identity
- betaと異なるdeployment / volume / world
- loopback bindとbetaと異なるport

## 4. bootstrap apply

reviewしたlock identityを手入力する。

```bash
REVIEWED_LOCK_IDENTITY="sha256:<planで確認した64-hex>"

uv run mcrctl apply \
  --project "$MC_REMOTE_PROJECT" \
  --output "$MC_REMOTE_PROJECT/generated" \
  --expected-lock-identity "$REVIEWED_LOCK_IDENTITY" \
  --docker-context default \
  --bootstrap \
  --yes \
  --allow-unverified
```

applyはalpha以外のcontainerを停止せず、未知container / volume、port衝突、remote Docker contextを
fail closedにする。既存betaを止める必要がある場合は、このcommandの外で人間が明示判断する。

## 5. read-only確認とevidence

```bash
uv run mcrctl doctor --project "$MC_REMOTE_PROJECT"
```

最低限、current lock / canonical render、managed volume、healthy container、loopback限定port、
protocol / Minecraft version、tokenなしhelloを確認する。beta project / volume / world bytesが
変わっていないことも別途確認する。

正式compatibility根拠に使う場合、private host、IP、OS user、absolute path、token、pair code、
player UUIDを除いたsanitized transcriptとrecord draftを作り、knowledge ownerへhandoffする。
このrepoを担当するagentはknowledge repoへ直接commit / pushしない。
