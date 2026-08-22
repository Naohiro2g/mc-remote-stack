# 通常dev環境 host-native runbook

このrunbookは、開発者workstationから別のserver host上にあるMcRemote dev runtimeを使うための、
現在有効な最短経路を示す。論理identityは次のとおりである。

- deployment／environment: `dev-integration`
- channel: `dev`
- purpose: `integration`
- exposure: `lan-only`

物理host、SSH alias、private addressはbackstageのprivate inventoryだけで管理し、この公開文書へ固定しない。

現在の通常dev環境はDocker／Composeを使わないhost-native構成である。PaperとMcRemoteをhost Java
21で直接起動し、service managerにはsystemdを使う。旧Docker経路のorder／lock／profileは履歴であり、
このrunbookの入力にしない。

```text
開発者workstation                         server host
Minecraft client ─── LAN/TCP 25565 ───> Paper
Scratch browser ──> local Bridge ──────> McRemote TCP 25575
Python process ─────────────────────────> McRemote TCP 25575
WireScope browser <── workstation内のMessageChannel／loopback station
```

GUI、browser、Minecraft Launcherをserver hostへ導入しない。Minecraft client、Scratch、Python、
WireScopeは開発者workstation側で実行する。

knowledge contractは`2026-08-21-03`／`2026-08-21-04`、host-native bootstrap修正の参照commitは
`540056048657ee0b27f8ccfe3626798b7c433ac7`である。gate coordinatorがauthorized next actionと
exact artifactを示していない場合、artifact配置とruntime起動を行わずgate coordinatorへ戻す。

## 1. exact入力をoperator stagingへ置く

releaseごとにgate coordinatorから次を受け取る。

- Paper JARとSHA-256
- McRemote JARとSHA-256
- protocol version
- `server.properties`
- McRemote `config.yml`
- 人間が承認した`eula.txt`

未push worktree、`build/libs`、`/tmp`、movingなlatestをartifact取得元にしない。review済みbytesを
operator所有のstaging directoryへ置き、toolへfile pathとdigestを明示する。次はb5 gateで固定した値である。

```sh
MC_REMOTE_STACK="$HOME/mc-remote-stack"
HOST_NATIVE_TOOL="$MC_REMOTE_STACK/tools/host-native-dev-runtime.sh"
HOST_NATIVE_STAGING="$HOME/mc-remote-host-native-staging"

PAPER_JAR="$HOST_NATIVE_STAGING/paper.jar"
PAPER_SHA256="5ffef465eeeb5f2a3c23a24419d97c51afd7dbb4923ff42df9a3f58bba1ccfba"
MCREMOTE_JAR="$HOST_NATIVE_STAGING/mc-remote.jar"
MCREMOTE_SHA256="17cdc457a886dd1d37c8e969e5406016460599636f55fdeb584af0012c61aeb6"
PROTOCOL_VERSION="22.0.0"
```

`config.yml`のcredential backendは同じfileやdirectoryを共有せず、次の独立pathを使う。

```yaml
auth:
  credential_store_path: "/srv/mc-remote/dev-integration/credential-store/snapshot.json"
  revocation_authority_path: "/srv/mc-remote/dev-integration/credential-revocations"
```

## 2. read-only preflight

最初にrootを使わず入力とhost toolchainを検査する。
正準入口は`tools/host-native-dev-runtime.sh check`、新規構築は
`tools/host-native-dev-runtime.sh install`、中断後の再開は
`tools/host-native-dev-runtime.sh verify`である。

```sh
"$HOST_NATIVE_TOOL" check \
  --paper "$PAPER_JAR" \
  --paper-sha256 "$PAPER_SHA256" \
  --mcremote "$MCREMOTE_JAR" \
  --mcremote-sha256 "$MCREMOTE_SHA256" \
  --config "$HOST_NATIVE_STAGING/config.yml" \
  --server-properties "$HOST_NATIVE_STAGING/server.properties" \
  --eula "$HOST_NATIVE_STAGING/eula.txt" \
  --protocol "$PROTOCOL_VERSION"
```

成功条件は、両artifactのhash一致、Java 21、必要command、EULA、credential backend path、旧Docker
runtime非稼働である。25565／25575に既存listenerがあれば停止する。backstage管理下のhostでは、
全service／listenerをbackstage inventoryで所有者、用途、期待状態を確定し、未知のlistenerを許容しない。

## 3. 新規構築と明示bootstrap

preflight後、人間operatorが一度だけroot modeを実行する。

```sh
sudo "$HOST_NATIVE_TOOL" install \
  --paper "$PAPER_JAR" \
  --paper-sha256 "$PAPER_SHA256" \
  --mcremote "$MCREMOTE_JAR" \
  --mcremote-sha256 "$MCREMOTE_SHA256" \
  --config "$HOST_NATIVE_STAGING/config.yml" \
  --server-properties "$HOST_NATIVE_STAGING/server.properties" \
  --eula "$HOST_NATIVE_STAGING/eula.txt" \
  --protocol "$PROTOCOL_VERSION"
```

toolは次を一巡する。

1. 既存runtimeを停止し、Docker runtimeと標準portが空であることを確認する。
2. 既存treeを日時付きfailure archiveへ移す。失敗treeをその場でchownして合格扱いにしない。
3. Paper／McRemote JARをroot所有のimmutable入力として配置する。
4. world、plugin runtime data、生成・更新されるconfig、credential backendを専用service account所有にする。
5. systemd unitを作り、`WorkingDirectory=/srv/mc-remote/dev-integration/data`を固定する。
6. 同じworking directoryで一時Paperを起動し、plugin所有console command
   `mcremote credential bootstrap`を送る。
7. `mcremote credential status`が`Credential domain: HEALTHY`を返すことを確認する。
8. 一時Paperを正常停止し、systemd起動、tokenなしhello、通常restart、同一domainを確認する。

credential snapshotやauthority内部JSONをStackが生成しない。bootstrap commandの成功後も、
credentialのHEALTHYだけではruntime readyとしない。次の三つの新規起動ログと実listenerをすべて待つ。

- `Credential domain health: HEALTHY (healthy)`
- `Server started at port 25575`
- `Done (`
- 25565／25575の実listener

各systemd起動はjournal cursor以後のログだけで判定する。古いHEALTHYやDoneを再利用しない。

## 4. 中断後の検証再開

installがcredential bootstrap後の検証段階で停止した場合、全installを繰り返さない。原因を直して既存の
snapshot／authorityを保持し、次だけを実行する。

```sh
sudo "$HOST_NATIVE_TOOL" verify \
  --paper-sha256 "$PAPER_SHA256" \
  --mcremote-sha256 "$MCREMOTE_SHA256" \
  --protocol "$PROTOCOL_VERSION"
```

verifyはruntime再作成やcredential再bootstrapを行わない。artifact、service accountのread／write境界、
credential domainを確認してから、完全ready、tokenなしhelloの`auth_required`、正常restart、別PID、
同一domain、再度の完全readyを検査する。

失敗時に同じcommandを推測再実行しない。toolはserviceを停止し、表示したbootstrap logまたは
`journalctl -u mc-remote-dev-integration.service`から最初のfailure reasonを確認してgate coordinatorへ戻す。

## 5. host返却

tool成功後、Stack担当は次をread-onlyで返す。

- systemd serviceが専用service accountでactive
- Java 21、Paper／McRemote version、両artifact SHA-256
- credential domainがHEALTHYで、通常restart後も同一domain
- 25565／25575の実listener
- 開発者workstationから両portへLAN到達
- workstationからのtokenなしhelloが`auth_required`
- Docker runtime非稼働
- backstage inventory上の既知service／listenerだけで、未知service／listenerなし

Minecraft player、pairing、Scratch、Python、WireScopeを使う製品APIのlive-auto／live-humanは、このhost返却に
含めない。environment readiness返却後、gate coordinatorが別の統一実施票で開始する。

## 6. 保存対象と再構築境界

このdev runtimeのworld、session、pairing、credentialは使い捨て可能な検証状態である。release更新時に守る
第一対象は、Scratch projectやPython sourceとしてworkstation側へ保存した建築コードである。古いruntimeを
完全互換のまま動かし続けることをgateにしない。必要なコード書換え方法をrelease note／教材へ示したうえで、
coordinatorが固定した新しいartifact一組から再構築できることを優先する。
