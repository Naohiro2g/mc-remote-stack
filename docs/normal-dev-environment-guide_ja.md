# 通常dev topology — host-native方式 runbook

このrunbookは、通常dev topologyをhost-native方式で構築したenvironmentに適用する。Paper／McRemoteはserver hostで直接実行する。
Minecraft client、Scratch、Bridge、Python、WireScopeは開発者workstationで動かし、Scratch／Bridgeはloopbackを使う。
ケータリング型で構築する通常devは、対応profileのcompact `apply`／`doctor`を使う。

- logical deployment／environment: `dev-integration`
- channel: `dev`
- purpose: `integration`
- exposure: `lan-only`
- launcher: `run.sh`
- console: 名前付きScreen session

物理host、SSH接続先、server root、session名、portはbackstage inventoryを正本とする。Stack担当はinventoryを
読み、同じ値を実機のread-only preflightで確認してから、authorized next actionの範囲を実行する。

## 1. handoffを受け取る

handoffには次を一組で入れる。

| 値 | 所有元 |
| --- | --- |
| `MC_REMOTE_TARGET`、`SERVER_ROOT`、`SCREEN_SESSION` | backstage inventory |
| `JAVA_PORT`、`MCREMOTE_PORT`、`PAPER_JAR`、`CURRENT_MCREMOTE_JAR` | backstage inventory＋実機preflight |
| `MCREMOTE_TAG`、`MCREMOTE_ASSET`、`MCREMOTE_URL`、`MCREMOTE_SHA256` | component release handoff＋Stack artifact照合 |
| `EXPECTED_PAPER_SHA256`、`EXPECTED_JAVA_MAJOR`、`EXPECTED_PROTOCOL` | exact release set |
| `authorized next action` | human operator（release済みset）／gate coordinator（candidate） |

公開artifactの取得元とdigestは
[`release artifact／preset準備runbook`](release-preset-preparation-guide_ja.md)で照合する。このhost-native
runbookは、そのartifact handoffをserverへ適用する。private実値を公開repoや作業結果へ転記しない。

管理端末から対象hostへ入り、handoff値を設定する。

```sh
MC_REMOTE_TARGET="<backstage handoff>"
ssh "$MC_REMOTE_TARGET"

SERVER_ROOT="<backstage handoff>"
SCREEN_SESSION="<backstage handoff>"
JAVA_PORT="<backstage handoff>"
MCREMOTE_PORT="<backstage handoff>"
PAPER_JAR="<backstage handoff>"
CURRENT_MCREMOTE_JAR="<backstage handoff>"
```

## 2. read-only preflight

server hostで現行runtimeと入力を確認する。この段階では稼働中serverを継続する。

```sh
test -d "$SERVER_ROOT"
test -x "$SERVER_ROOT/run.sh"
test -f "$PAPER_JAR"
test -f "$CURRENT_MCREMOTE_JAR"
test -w "$SERVER_ROOT/plugins"
command -v curl
command -v java
command -v screen
command -v sha256sum
command -v ss
java -version
sha256sum "$PAPER_JAR" "$CURRENT_MCREMOTE_JAR"
screen -ls || true
ss -ltn | grep -E ":($JAVA_PORT|$MCREMOTE_PORT)[[:space:]]" || true
find "$SERVER_ROOT/plugins" -maxdepth 1 -type f \
  \( -iname '*mcremote*.jar' -o -iname 'mc-remote*.jar' \) -print
```

Paperは`EXPECTED_PAPER_SHA256`、現在のMcRemoteはinventoryのcurrent identityへ一致させる。Screen session、
Java process、両listenerを同じruntimeへ対応付ける。inventoryと実機が異なる場合は、観測結果をbackstageの
更新対象として返し、対応関係を確定してから同じpreflightを行う。

## 3. artifact staging

target artifactはserver停止前に取得して検証する。

```sh
MCREMOTE_TAG="<artifact handoff>"
MCREMOTE_ASSET="<artifact handoff>"
MCREMOTE_URL="<artifact handoffのGitHub Release asset URL>"
MCREMOTE_SHA256="<artifact handoff>"
EXPECTED_PAPER_SHA256="<exact release set>"
EXPECTED_JAVA_MAJOR="<exact release set>"
EXPECTED_PROTOCOL="<exact release set>"
STAGING_ROOT="$HOME/.local/share/mc-remote/host-native-staging/$MCREMOTE_SHA256"

install -d -m 0750 "$STAGING_ROOT"
curl --fail --location --output "$STAGING_ROOT/$MCREMOTE_ASSET" "$MCREMOTE_URL"
test "$(sha256sum "$STAGING_ROOT/$MCREMOTE_ASSET" | awk '{print $1}')" = \
  "$MCREMOTE_SHA256"
test "$(sha256sum "$PAPER_JAR" | awk '{print $1}')" = \
  "$EXPECTED_PAPER_SHA256"
```

preflightのcurrent identity、target tag／asset／SHA-256、維持するPaper／world／config／credential、停止予定の
Screen sessionとportを確認し、authorized next actionと一致させる。
現在のMcRemote JARが既に`MCREMOTE_SHA256`と一致する場合はJAR交換を行わず、§5のreadinessを確認して
`result: unchanged`で完了する。

## 4. 正常停止と一件交換

consoleへ`stop`を送り、world保存とplugin shutdownを完了させる。

```sh
if screen -ls | grep -Fq ".$SCREEN_SESSION"; then
  screen -S "$SCREEN_SESSION" -p 0 -X stuff $'stop\r'
fi
for attempt in $(seq 1 120); do
  if ! screen -ls | grep -Fq ".$SCREEN_SESSION" && \
     ! ss -ltn | grep -Eq ":($JAVA_PORT|$MCREMOTE_PORT)[[:space:]]"; then
    break
  fi
  sleep 1
done
! screen -ls | grep -Fq ".$SCREEN_SESSION"
! ss -ltn | grep -Eq ":($JAVA_PORT|$MCREMOTE_PORT)[[:space:]]"
```

停止確認後、現在のJARをoperator backupへ移し、検証済みJARを一件だけ配置する。

```sh
BACKUP_ROOT="$SERVER_ROOT/operator-backup/$(date +%Y%m%dT%H%M%S)"
BACKED_UP_MCREMOTE_JAR="$BACKUP_ROOT/$(basename "$CURRENT_MCREMOTE_JAR")"
TARGET_MCREMOTE_JAR="$SERVER_ROOT/plugins/$MCREMOTE_ASSET"

install -d -m 0750 "$BACKUP_ROOT"
mv "$CURRENT_MCREMOTE_JAR" "$BACKED_UP_MCREMOTE_JAR"
install -m 0644 "$STAGING_ROOT/$MCREMOTE_ASSET" "$TARGET_MCREMOTE_JAR"
test "$(sha256sum "$TARGET_MCREMOTE_JAR" | awk '{print $1}')" = "$MCREMOTE_SHA256"
test "$(find "$SERVER_ROOT/plugins" -maxdepth 1 -type f \
  \( -iname '*mcremote*.jar' -o -iname 'mc-remote*.jar' \) | wc -l)" -eq 1
```

このwrite setはMcRemote JARとoperator backupだけである。Paper、world、server properties、McRemote config、
credential store、revocation authorityを同じ操作で変更しない。

## 5. 起動とreadiness

server rootの正準launcherを実行する。

```sh
cd "$SERVER_ROOT"
./run.sh
```

`run.sh`が開いたconsoleを維持する。consoleから離れる場合はScreenのdetach操作`Ctrl-A`、`D`を使う。
起動後、次を確認する。

```sh
screen -ls | grep -F ".$SCREEN_SESSION"
test "$(sha256sum "$TARGET_MCREMOTE_JAR" | awk '{print $1}')" = "$MCREMOTE_SHA256"
grep -E 'Starting minecraft server version|This server is running Paper|Done \(' \
  "$SERVER_ROOT/logs/latest.log" | tail -n 3
grep -Ei 'McRemote|Credential domain' "$SERVER_ROOT/logs/latest.log" | tail -n 20
ss -ltn | grep -E ":($JAVA_PORT|$MCREMOTE_PORT)[[:space:]]"
```

完了条件は、期待するPaper／McRemote version、`Credential domain health: HEALTHY`、両listener、workstationからの
到達、tokenなし`hello`の`auth_required`、必要なpairing／認証済み代表callである。release gateの追加試験は
指定されたexact setと実施票で続ける。

起動またはreadinessが失敗した場合はconsoleを正常停止し、target JARを同じbackup directoryへ移して
`BACKED_UP_MCREMOTE_JAR`を元のpathへ戻す。`run.sh`で再起動し、この節のreadinessで旧runtimeの復帰を確認する。

## 6. handoffを完了する

```text
target: <backstage上の参照>
runtime: host-native run.sh / Screen
release: <MCREMOTE_TAG>
paper sha256: <EXPECTED_PAPER_SHA256との一致>
mcremote sha256: <MCREMOTE_SHA256との一致>
credential health: HEALTHY
listeners: expected
protocol auth: auth_required
result: updated / unchanged / restored
```

private host値、token、credential内容、raw logはbackstageまたはGit外の所定位置へ返す。
