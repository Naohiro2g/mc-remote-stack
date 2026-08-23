# 通常dev環境 runbook

通常dev環境は、人間がMinecraft server consoleを直接操作しながら、開発者workstationから
Minecraft、Scratch、Python、WireScopeを一巡するためのhost-native環境である。

この文書は現在有効な操作と確認点だけを示す。b5 gate中に試したDocker／Compose版とsystemd版は、
通常devの現行起動経路ではない。将来Stackへ自動化を追加するときも、この人間操作を失わせない。

knowledge contractは`2026-08-21-03`／`2026-08-21-04`。b5の技術gateとprerelease identity確認は
2026-08-23に完了した。以後のrelease gateは、knowledgeのgate coordinatorが示すexact setと
authorized next actionに従う。

## 1. 現行identityとtopology

- logical deployment／environment: `dev-integration`
- channel: `dev`
- purpose: `integration`
- exposure: `lan-only`
- server root: `/home/tsuji/MINECRAFT_SERVERS/PaperMC`
- launcher: `run.sh`
- console: Screen session `Minecraft server`
- Minecraft Java port: `25565`
- McRemote port: `25575`

server hostではPaper／McRemoteを直接動かす。Minecraft client、Scratch browser、Bridge、Python、
WireScope browserは開発者workstation側で動かす。

```text
開発者workstation                         server host
Minecraft client ─── LAN/TCP 25565 ───> Paper + McRemote
Scratch browser ──> local Bridge ──────> McRemote TCP 25575
Python process ─────────────────────────> McRemote TCP 25575
WireScope browser <── workstation内のMessageChannel／loopback station
```

ケータリングprofileは別topologyであり、この通常dev環境へ暗黙に追加しない。

## 2. b5で確認したexact runtime

- protocol: `22.0.0`
- artifact version: `2200.0.0b5`
- Minecraft: `1.21.11`
- Paper: `1.21.11-132`
- Paper JAR SHA-256: `5ffef465eeeb5f2a3c23a24419d97c51afd7dbb4923ff42df9a3f58bba1ccfba`
- McRemote source: `bbbb53602a9c375e2ead3ee4c22174d5cf424f55`
- McRemote JAR SHA-256: `f7ddbcb5a92acadfe1adb7a9f6a4f50a05707e2eefbd1c01ff9aeeebe0a36547`

b5の横断exact compatibility setと正式evidenceはknowledge
`00-hub/release-gate-notes_ja.md`を正とする。このrunbookから他componentのidentityや次releaseの候補を
推測しない。

## 3. 起動前確認

server hostで次を確認する。

```sh
SERVER_ROOT=/home/tsuji/MINECRAFT_SERVERS/PaperMC

test -d "$SERVER_ROOT"
test -x "$SERVER_ROOT/run.sh"
systemctl is-active mc-remote-dev-integration.service || true
screen -ls || true
ss -ltn | grep -E ':(25565|25575)[[:space:]]' || true
find "$SERVER_ROOT/plugins" -maxdepth 1 -type f -name 'mc-remote-*.jar' -print
```

成功条件は次である。

- 旧systemd版は停止しており、`active`ではない。
- 既存の`Minecraft server` sessionがない、または既に意図したruntimeとして稼働している。
- 新規起動前は`25565`／`25575`が空いている。
- `plugins`直下のMcRemote JARはexactな1本だけである。

未知のservice、listener、JARを別portや追加JARで回避しない。所有者と用途を確認して停止する。

## 4. 起動とconsole操作

新規起動はserver rootから行う。

```sh
cd /home/tsuji/MINECRAFT_SERVERS/PaperMC
./run.sh
```

consoleへ入る。

```sh
screen -r "Minecraft server"
```

Screenから抜けるだけなら`Ctrl-A`、続いて`D`を使う。serverを止める場合はconsoleで`stop`を実行し、
world保存とplugin shutdownを完了させる。Screen processやJava processの強制終了を通常手順にしない。

通常devでは`op`、credential bootstrap／statusなどのoperator commandもこのconsoleから実行できる。
pair code、token、credential内容を公開logや本repoへ保存しない。

## 5. 起動後readiness

```sh
SERVER_ROOT=/home/tsuji/MINECRAFT_SERVERS/PaperMC

screen -ls
grep -E 'Starting minecraft server version|This server is running Paper|Done \(' \
  "$SERVER_ROOT/logs/latest.log" | tail -n 3
grep -Ei 'McRemote|Credential domain' "$SERVER_ROOT/logs/latest.log" | tail -n 20
ss -ltn | grep -E ':(25565|25575)[[:space:]]'
```

最低限の成功条件は次である。

- PaperとMcRemoteが期待versionで起動している。
- `Credential domain health: HEALTHY`である。
- `25565`／`25575`がLISTENしている。
- workstationからLAN到達できる。
- tokenなしprotocol helloが`auth_required`になる。

このreadinessは製品API試験の代わりではない。release gateのlive-auto／live-humanは、coordinatorが指定した
exact set、test ID、順序、証跡境界で別に実施する。

## 6. McRemote JAR交換

JAR交換はserverを正常停止してから行う。担当worktreeのmovingな`build/libs`を直接参照せず、coordinatorが
固定したsource commit、filename、bytes、SHA-256と一致するreview済みbytesを使う。

1. source側とserver staging側でSHA-256を確認する。
2. consoleの`stop`でserverを停止し、Screen sessionと両listenerの終了を確認する。
3. 旧JARを`plugins`外のoperator backupへ退避する。
4. `plugins`直下へ新JARを1本だけ配置する。
5. 配置後SHA-256とJAR件数を再確認する。
6. `run.sh`で起動し、§5のreadinessを確認する。

config、world、credential store、revocation authorityをJARと一緒に巻き戻さない。保護対象とrollback境界は
そのreleaseのknowledge contractを確認する。

## 7. 使用しない経路

次はb5準備中の試行または失効した案であり、通常devの現在有効な操作には使わない。

- `home-server@5`／`compose@5`／Docker order・lock・renderを通常devへapplyする経路
- `/srv/mc-remote/dev-integration`をsystemd service accountで直接起動する経路
- Stack draft PR #29 `codex/host-native-dev-runbook`
- backstage draft PR #5に記録されたsystemd runtime inventory
- release担当の記憶によるartifact選択、未push commit、`/tmp` artifact、movingな`latest`

`home-server@5`やreview済みCAS importなどの実装事実は消さないが、通常devの現行起動経路ではない。

## 8. 後続の自動化設計

後続の自動化設計は別sliceで行う。最低条件は次である。

- `MINECRAFT_SERVERS/PaperMC`とScreen consoleを使う人間の操作性を維持する。
- exact artifactの取得、SHA検査、単一JAR配置、readinessを自動化する。
- 実行中runtime、未知listener、重複JARを停止前preflightで検出する。
- artifact更新とserver起動を分離し、人間が内容を確認できる。
- 通常経路と障害対応を分離し、release固有の作業日誌をrunbookへ積み重ねない。
- backstage inventoryを物理host／service／portの正本とし、公開repoへprivate実値を置かない。

汎用profile、renderer、destroy／完全rollback framework、ケータリング対応を、この最短runbookの前提にしない。
