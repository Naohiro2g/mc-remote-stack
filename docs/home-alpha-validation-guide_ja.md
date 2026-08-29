# home private alpha 検証ガイド

## 0. 目的と境界

このガイドは、home private betaを変更せず、別のproject / volume / world / loopback portで
private alphaをbootstrapする。b3 component内容の検証ではなく、alpha orderとケータリング型
deployment経路の検証である。

対象は次のexact contractに限定する。

| axis | value |
| --- | --- |
| profile | `home-server@4` |
| preset | `mcremote-paper@2` |
| channel | `alpha` |
| exposure | `isolated` |
| purpose | `integration` |
| renderer | `compose@6` |

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
  --profile home-server@4 \
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
- required security controlが`mcremote-auth-enforced`
- generated treeに`minecraft/plugins/McRemote/config.yml`があり、`auth.enforcement=true`

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
protocol endpointがtokenなしhelloを`auth_required`で拒否することを確認する。成功した場合は
`doctor_auth_not_enforced`でFAILする。beta project / volume / world bytesが変わっていないことも
別途確認する。

既存`home-server@2` lockは自動更新しない。新profileへの移行は新lockとcanonical renderを先に
reviewし、既存worldを保持する専用transactionで行う。旧checkoutのままdoctorを実行したり、
生成外の設定だけを手編集してmigration完了と扱わない。

既存alphaのread-only planは次で生成する。target volumeは旧volumeと異なる明示identityにする。

```bash
TARGET_ALPHA_VOLUME="home-alpha-auth-minecraft-data"

uv run mcrctl migration auth-enforcement plan \
  --project "$MC_REMOTE_PROJECT" \
  --output "$MC_REMOTE_PROJECT/generated" \
  --docker-context default \
  --target-volume "minecraft-data=$TARGET_ALPHA_VOLUME" \
  --allow-unverified
```

表示されたsource / target lockをreviewした後、`apply`へ両identity、同じtarget volume、`--yes`を渡す。
applyはimage取得と新volume作成を済ませてから旧alphaだけを停止し、新desired stateをpublishして
volumeをcopyする。
その後`home-server@4`を起動し、token無しhelloが`auth_required`になるまでdoctorする。

失敗時に旧runtimeは自動再起動しない。`.mcrctl/migrations/auth-enforcement/state.json`へ残ったphaseを
確認し、原因を修理して同じapply commandを再実行する。旧volumeは成功後も自動削除しない。CLIの
隔離試験は実装済みだが、このhome alpha hostへのlive適用はまだ実施していない。

正式compatibility根拠に使う場合、private host、IP、OS user、absolute path、token、pair code、
player UUIDを除いたsanitized transcriptとrecord draftを作り、knowledge ownerへhandoffする。
このrepoを担当するagentはknowledge repoへ直接commit / pushしない。

## 6. home-server@6 フルスタック版（Caddy/Scratch/Bridge、tailnet経由）

設計正本は
[`docs/home-alpha-full-stack-profile-design_ja.md`](home-alpha-full-stack-profile-design_ja.md)。
上記§1〜§5のMinecraft単体alpha（`home-server@4`）とは別のproject
（`home-alpha-full`等、既存のisolated alphaや home betaと共存する別project /
volume / world / port）として構築する。§1のbeta分離原則をそのまま適用する。

対象contractは次のexact値に限定する。

| axis | value |
| --- | --- |
| profile | `home-server@6` |
| preset | `home-alpha-full@1`（以降append-onlyで更新、design doc §7） |
| channel | `alpha` |
| exposure | `isolated` |
| renderer | `compose@14` |

### 6.1 初期化とoperator input

```bash
MC_REMOTE_PROJECT="$HOME/mc-remote-deployments/home-alpha-full"

uv run mcrctl init "$MC_REMOTE_PROJECT" \
  --format toml \
  --deployment-name home-alpha-full \
  --profile home-server@6 \
  --environment-identity home-alpha-full \
  --channel alpha \
  --exposure isolated \
  --purpose integration \
  --preset home-alpha-full@1 \
  --artifact-store "$HOME/.local/share/mc-remote/artifacts" \
  --volume minecraft-data=home-alpha-full-minecraft-data \
  --volume caddy-data=home-alpha-full-caddy-data \
  --volume caddy-config=home-alpha-full-caddy-config \
  --world-identity home-alpha-full-world \
  --bind-address 127.0.0.1 \
  --java-port 25567 \
  --mcremote-port 25577

uv run mcrctl accept-eula --project "$MC_REMOTE_PROJECT" --yes
```

`network.bind_address`は**必ずloopback**にする。`compose@14`はCaddy／Minecraftを
loopbackだけへbindする設計であり（§4、`toml_project.py`の`isolated`露出契約に
従う）、tailnetへの到達性はhost側の`tailscale serve`（6.3）が別途担う。

`lan-routes@1` operator inputを手編集で追加する。`hostname`は対象host自身の
Tailscale MagicDNS名、`scratch_port`/`bridge_port`はCaddyがloopbackで listenする
port番号（同じ番号をそのまま`tailscale serve`のtailnet側公開portにも使う、6.3）。

```bash
cat >> "$MC_REMOTE_PROJECT/mc-remote.toml" <<'EOF'

[[operator_inputs]]
role = "lan-routes"
adapter = "lan-routes@1"
path = "operator/lan-routes/routes.toml"
EOF

mkdir -p "$MC_REMOTE_PROJECT/operator/lan-routes"
cat > "$MC_REMOTE_PROJECT/operator/lan-routes/routes.toml" <<'EOF'
hostname = "<この host の Tailscale MagicDNS 名>"
scratch_port = 8443
bridge_port = 8444
EOF
```

`mc-remote.toml`の`[acknowledgements]`を人間が編集し、`home-alpha-full@1`が
現時点で`unverified`である理由を記録する（§6の`home-alpha-full-stack-profile-
design_ja.md`が記す通り、developer側に新しいcommitが積まれるまではbetaと同内容）。

```toml
[acknowledgements]
allow_unverified = true
unverified_reason = "home-server@6 full-stack alpha initial live validation"
allow_eol = false
eol_reason = ""
```

### 6.2 resolve / plan / render / apply

```bash
uv run mcrctl resolve --project "$MC_REMOTE_PROJECT" --allow-unverified
uv run mcrctl plan --project "$MC_REMOTE_PROJECT"
uv run mcrctl artifact fetch --project "$MC_REMOTE_PROJECT"
uv run mcrctl render --project "$MC_REMOTE_PROJECT" --output "$MC_REMOTE_PROJECT/generated"
```

`plan`が表示するlock identityをreviewしてから、`docs/home-alpha-validation-guide_ja.md`
§4と同じ形で`apply --bootstrap --yes --allow-unverified`する。applyはCaddy／
Scratch／Bridge／Minecraftの4 serviceをすべてloopbackへ起動する。この時点では
tailnetから一切到達できない（想定どおり）。

### 6.3 `tailscale serve`によるtailnet到達性の付与（host側、Stackの管轄外）

Stackはこの節のcommandを実行・検証しない。対象host上で運営者が実行する。
tailnet側公開portは、6.1で`lan-routes@1`へ書いた`scratch_port`/`bridge_port`、
および`network.java_port`と**同じ番号**を使う（Stack側と揃える運用、design doc
§8）。

```bash
# Scratch（Caddy経由、HTTPSでTLS終端はtailscale serveが担う）
tailscale serve --bg --https=8443 http://127.0.0.1:8443

# Bridge（Caddy経由、wss upgradeを含む）
tailscale serve --bg --https=8444 http://127.0.0.1:8444

# Minecraft本体（生Javaプロトコル、raw TCP forwarding）
tailscale serve --bg --tcp=25567 tcp://127.0.0.1:25567

tailscale serve status
```

`tailscale serve --tcp`はSSH/RDB等と同様にraw TCPをそのまま転送する
（[公式docs](https://tailscale.com/docs/reference/tailscale-cli/serve)）。HTTPSモードは
Tailscaleが自動発行するtailnet証明書でTLS終端し、Stack側のCaddyはTLSを持たない
（design doc §4）。

### 6.4 read-only確認

```bash
uv run mcrctl doctor --project "$MC_REMOTE_PROJECT"
```

`doctor`が確認できるのはStack管理下のcontainer／volume／lock整合性までであり、
`tailscale serve`が正しく転送しているかはhost側で別途確認する（例:
tailnet参加端末から`https://<hostname>:8443`／`:8444`とMinecraft clientでの
接続を試す）。sanitized live-human evidenceの扱いは§0・§5と同じ。

### 6.5 presetの更新（append-only、非自動）

手順は
[`home-alpha-full-stack-profile-design_ja.md`§7](home-alpha-full-stack-profile-design_ja.md#7-更新手続き都度選び直し非自動)。
McRemote / scratch-editorのHEADが動いた時だけ新しい`home-alpha-full@N+1`を作る。
`home-server@6`／`lan-routes@1`／`compose@14`自体は変更不要。

**既存deploymentへの適用は`apply --bootstrap`ではない**（それは初回専用）。
同一volumeのまま更新する`mcrctl deployment update plan/apply`を使う（design doc
§7手順4）。
