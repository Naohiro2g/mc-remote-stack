# McRemote b3 credential isolated alpha 検証ガイド

## 0. 目的と境界

公開済みMcRemote `2100.0.0b3`を、重要dataを持たないisolated alphaへexact-pinして検証する。
これは一般運用のbootstrap契約ではなく、`unverified` acknowledgement付きのcontrolled live testである。

| axis | value |
| --- | --- |
| profile | `home-server@3` |
| preset | `mcremote-paper@3` |
| channel / exposure / purpose | `alpha` / `isolated` / `integration` |
| renderer | `compose@5` |
| McRemote SHA-256 | `aeb190705bd9957ce73557dc1be0fe15efe7250ba9bc688945e6f537e00ef78e` |

このsliceではfresh apply、明示bootstrap、正常restart、token無しhello、pairingとcredential再接続までを
確認する。`credential reset`、snapshot rollback、既存environment migration、公開networkは行わない。
credential checkpointが未実装なのでcompatibilityは`unverified`のままとし、通常のdoctor成功や一般運用
readinessを主張しない。

## 1. 人間checkpoint

apply前にoperatorが次を確認する。

- 対象は再構築可能な検証host、または既存host上の完全に別のprojectである。
- deployment、三volume、world、Java / McRemote portが既存環境と重複しない。
- host memory、swap、diskとportに余裕があり、既存環境を停止する必要がない。
- Minecraft EULAと`unverified`理由を確認した。
- reviewしたlock identityをapply時に再入力する。

private host、IP、OS user、absolute path、credentialをrepositoryへ記録しない。

## 2. project作成

以下の名前とportは例であり、private inventoryと照合してから使用する。

```bash
MC_REMOTE_PROJECT="$HOME/mc-remote-deployments/home-b3-alpha"

uv run mcrctl init "$MC_REMOTE_PROJECT" \
  --format toml \
  --deployment-name home-b3-alpha \
  --profile home-server@3 \
  --environment-identity home-b3-alpha \
  --channel alpha \
  --exposure isolated \
  --purpose integration \
  --preset mcremote-paper@3 \
  --artifact-store "$HOME/.local/share/mc-remote/artifacts" \
  --volume minecraft-data=home-b3-alpha-minecraft-data \
  --volume credential-store=home-b3-alpha-credential-store \
  --volume credential-revocations=home-b3-alpha-credential-revocations \
  --world-identity home-b3-alpha-world \
  --bind-address 127.0.0.1 \
  --java-port 25567 \
  --mcremote-port 25577

uv run mcrctl validate --project "$MC_REMOTE_PROJECT"
uv run mcrctl repo check --project "$MC_REMOTE_PROJECT"
uv run mcrctl accept-eula --project "$MC_REMOTE_PROJECT" --yes
```

`mc-remote.toml`へ一回限りの理由を記録する。

```toml
[acknowledgements]
allow_unverified = true
unverified_reason = "McRemote b3 isolated credential integration evidence"
allow_eol = false
eol_reason = ""
```

## 3. resolve / fetch / render / review

```bash
uv run mcrctl resolve --project "$MC_REMOTE_PROJECT" --allow-unverified
uv run mcrctl plan --project "$MC_REMOTE_PROJECT"
uv run mcrctl artifact fetch --project "$MC_REMOTE_PROJECT"
uv run mcrctl render \
  --project "$MC_REMOTE_PROJECT" \
  --output "$MC_REMOTE_PROJECT/generated"
```

planのwarningを成功へ読み替えない。operatorはprofile / preset、lock identity、artifact digest、loopback
port、三volume identity、`auth.enforcement=true`、credential pathが`/data`外であることをreviewする。

## 4. controlled applyと明示bootstrap

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

apply成功はcontainerとmount topologyの成立だけを意味し、credential domainの健康を意味しない。
canonical composeから対象containerを一意に解決し、container-local consoleへ一度だけ投入する。

```bash
CONTAINER_ID="$(docker --context default compose \
  --ansi never \
  --project-directory "$MC_REMOTE_PROJECT/generated" \
  --file "$MC_REMOTE_PROJECT/generated/compose.yaml" \
  ps --quiet minecraft)"
test -n "$CONTAINER_ID"
```

別のprivate terminalでcontainer logをfollowし、command結果を画面上だけで観測する。logをそのまま
evidence fileへ保存しない。

```bash
docker --context default logs --follow --tail 100 "$CONTAINER_ID"
```

元のterminalからcommandを送る。

```bash

docker --context default exec "$CONTAINER_ID" \
  mc-send-to-console "mcremote credential status"
docker --context default exec "$CONTAINER_ID" \
  mc-send-to-console "mcremote credential bootstrap"
docker --context default exec "$CONTAINER_ID" \
  mc-send-to-console "mcremote credential status"
```

人間は順に`UNINITIALIZED`、bootstrap成功、`HEALTHY`をconsoleで確認する。command returnや表示文字列を
Stackの恒久的な機械契約にしない。domain UUIDを含むraw出力は正式evidenceへ転記しない。

## 5. b3 smoke

1. canonical composeでMinecraft serviceだけを正常restartする。

   ```bash
   docker --context default compose \
     --ansi never \
     --project-directory "$MC_REMOTE_PROJECT/generated" \
     --file "$MC_REMOTE_PROJECT/generated/compose.yaml" \
     restart minecraft
   ```

2. `credential status`が人間観測で`HEALTHY`へ戻ることを確認する。
3. token無しhelloが`auth_required`になることを確認する。
4. Minecraft内でpairingし、`mcrl_` credentialを一つ発行する。
5. clientを切断・再接続し、同じcredentialが正常restart後も利用できることを確認する。
6. lock、runtime、三volume identity、loopback portが変わっていないことを確認する。

`mcrctl doctor`はmount topologyを検査した後、b3に機械可読checkpointがないため
`doctor_credential_health_unsupported`で停止する。これはこのsliceの既知の境界であり、doctor PASSへ
読み替えない。helloとlive-human結果は別の観測として記録する。

## 6. 証跡と停止条件

正式根拠に使う場合は`live-auto`と`live-human`を分ける。pair code、raw token、domain / player UUID、
private host、world名をredactし、raw JSONやcredential backend内容を保存しない。

次の場合はその場で停止し、resetやfile編集で修復しない。

- bootstrap前の状態が`UNINITIALIZED`以外
- bootstrap後またはrestart後に`HEALTHY`を人間確認できない
- 既存container / volume / portとの衝突
- credential pathまたはvolume identityがlockと異なる
- pluginが無認証fallbackする、またはtoken無しhelloが成功する
- container console以外の経路が必要になる

snapshot rollback、revoke継続、authority欠落、resetは、初回smokeの結果をreviewしてから別checkpointで行う。
