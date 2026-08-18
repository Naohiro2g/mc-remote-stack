# Public VPS bootstrap guide

Ubuntu系VPSへ、ケータリング型のexact order / lock / render / apply境界を使って
MinecraftとMcRemoteのpublic betaを構築する。provider、実IP、個人名、秘密値へ依存せず、
対象host上へagentをinstallしなくても人間のterminalだけで完走できる手順を基準にする。

## 0. 現在の完成範囲

現在実装済みの`vps-server@6` / `public-web-paper@2`は、b3のScratch、Bridge、
McRemoteをsession-only認証で固定し、次を一つのpublic bootstrap transactionとして扱う。

- exact OCI Caddy / Scratch / Bridge / Minecraft runtime、Paper JAR、McRemote JAR
- Caddyだけをpublic edgeへ接続し、backend間通信をinternal app networkへ限定
- Paper初回起動に必要なMojang取得のため、Minecraftだけを明示IPv4 egressへも接続
- HTTPS route、Scratch runtime config、Bridge origin / sandbox allowlist
- explicit public IPv4 bind
- managed world / Caddy state volume
- Minecraft EULA、unverified compatibility、exact lock review
- unknown container / volume、port衝突のfail-closed preflight
- 起動失敗時のcontainer rollbackとworld volume保持
- current render、全container、全volume、public port、認証必須helloのdoctor

次はまだ同じtransactionへ入っていないため、後続phaseで完成度を上げる。

- official homepage content artifact（現在はCaddyが明示的なhealth landingを返す）
- provider / host firewall、DNS、TLSの変更
- HTTPS / WSSの外部smokeを行うdoctor claim
- backup / restore、upgrade、既存world import、stable / beta排他切替
- 既存deploymentからのin-place migration

### 現行b2 VPSの停止境界

`official-public-beta`の現行観測は`vps-server@5` / `public-web-paper@1`相当のb2で、
recovery用の追加Composeと旧generated treeを使っている。新規host用の
`vps-server@6` / `public-web-paper@2`が解決・renderできることは、そのlive runtimeを
上書きしてよい根拠にならない。

b2からb3への更新に`--bootstrap`を使わない。先に人間のinteractive sudo checkpointで
live Docker inspect / doctorを取得し、現行のCompose file列、working directory、volume、
plugin / config mountを確定する。専用transactionは新しい3 volumeへsource bytesをcopyし、
review済みrecovery ComposeをSHA-256でsnapshotしたうえでb3を起動する。

```sh
MC_REMOTE_STACK="$HOME/mc-remote-stack"
MC_REMOTE_PROJECT="$HOME/mc-remote-deployments/official-public-beta"
MC_REMOTE_OUTPUT="$MC_REMOTE_PROJECT/generated"
AUTH_CONFIG_ROOT="$HOME/.config/mc-remote/runtime/official-public-beta/minecraft"
```

現行Minecraft containerをexactly oneまで絞り、Dockerが記録したCompose provenanceを確認する。

```sh
sudo docker ps \
  --filter "label=com.docker.compose.project=official-public-beta" \
  --filter "label=com.docker.compose.service=minecraft" \
  --format '{{.ID}}'

SOURCE_MINECRAFT_CONTAINER="<上で確認したexactly oneのcontainer ID>"
sudo docker inspect \
  --format '{{ index .Config.Labels "com.docker.compose.project.config_files" }}' \
  "$SOURCE_MINECRAFT_CONTAINER"
```

前段の`auth-enforcement` transactionから起動した現行runtimeでは、追加Composeのidentityは
原本の`recovery/` pathではなく、そのtransactionが固定したsnapshot pathである。内容が同じでも
原本へ置き換えず、inspect結果の順序どおりにexact pathを指定する。現在期待する2 pathは次である。
inspect結果が異なる場合は推測せず、そのruntimeを起動したreview済みfile列を先に確定する。

```sh
SOURCE_RECOVERY_COMPOSE="$MC_REMOTE_PROJECT/.mcrctl/migrations/auth-enforcement/preserved-compose/00-compose.recovery-plugins.yaml"
SOURCE_HOMEPAGE_COMPOSE="$MC_REMOTE_PROJECT/.mcrctl/migrations/auth-enforcement/preserved-compose/01-compose.homepage.yaml"

sudo "$MC_REMOTE_STACK/.venv/bin/mcrctl" migration public-b3 plan \
  --project "$MC_REMOTE_PROJECT" \
  --output "$MC_REMOTE_OUTPUT" \
  --docker-context default \
  --target-volume minecraft-data=official-public-beta-b3-minecraft-data \
  --target-volume caddy-data=official-public-beta-b3-caddy-data \
  --target-volume caddy-config=official-public-beta-b3-caddy-config \
  --preserve-compose-file "$SOURCE_RECOVERY_COMPOSE" \
  --preserve-compose-file "$SOURCE_HOMEPAGE_COMPOSE" \
  --auth-config-root "$AUTH_CONFIG_ROOT" \
  --allow-unverified
```

planが示すsource / target lock、volume copy、preserved compositionをreviewしてから適用する。

```sh
REVIEWED_SOURCE_LOCK="sha256:<planで確認したsource>"
REVIEWED_TARGET_LOCK="sha256:<planで確認したtarget>"
REVIEWED_COMPOSITION="sha256:<planで確認したpreserved-composition>"

sudo "$MC_REMOTE_STACK/.venv/bin/mcrctl" migration public-b3 apply \
  --project "$MC_REMOTE_PROJECT" \
  --output "$MC_REMOTE_OUTPUT" \
  --docker-context default \
  --target-volume minecraft-data=official-public-beta-b3-minecraft-data \
  --target-volume caddy-data=official-public-beta-b3-caddy-data \
  --target-volume caddy-config=official-public-beta-b3-caddy-config \
  --preserve-compose-file "$SOURCE_RECOVERY_COMPOSE" \
  --preserve-compose-file "$SOURCE_HOMEPAGE_COMPOSE" \
  --auth-config-root "$AUTH_CONFIG_ROOT" \
  --expected-source-lock-identity "$REVIEWED_SOURCE_LOCK" \
  --expected-target-lock-identity "$REVIEWED_TARGET_LOCK" \
  --expected-preserved-composition-identity "$REVIEWED_COMPOSITION" \
  --allow-unverified \
  --yes
```

transactionは`.mcrctl/migrations/public-b3/`へsource render、target candidate、Compose snapshot、
`source-auth-config.yml`を保存する。source volumeは削除しない。失敗時はphaseと理由を保存し、
同じlock identityと引数でresumeする。旧runtimeを自動起動せず、旧volumeやsnapshotを削除しない。

旧`official-vps` YAML fixtureは、Caddy / Scratch / Bridgeを含む回帰比較と過去構成の読取りに
残しているが、新規VPSのapply経路ではない。archiveやprivate inventoryをこのrunbookの
実行時依存先にしない。

## 1. 対象と人間checkpoint

作業前に人間が次を決める。

- 対象VPSと個人管理者SSH user
- 新規hostか、既存serviceを停止して置換するhostか
- worldを空で作ってよいか
- public Java / McRemote port
- maintenance開始、停止許容時間、provider consoleの復旧経路

既存worldやserviceに保存価値がない場合も、対象を推測して削除しない。read-only discoveryで
実対象を確定し、削除対象と再構築後の到達条件を人間がreviewしてからmutationへ進む。

## 2. read-only discovery

対象hostへ個人鍵でSSHし、mutation前の状態を記録する。

```sh
date --iso-8601=seconds
id
cat /etc/os-release
uname -r
uptime -s
free -h
df -h /
command -v git
command -v python3
command -v uv
command -v docker
sudo -v
sudo docker version
sudo docker compose version
sudo docker context inspect default
sudo ss -lntup
sudo systemctl --failed
```

期待値:

- Python 3.11以上
- Docker EngineとCompose v2
- `default` contextのDocker endpointが対象hostのlocal Unix socket
- root/password loginを閉じる前に、別terminalの個人管理者SSHと`sudo -v`が成功

停止条件:

- target、SSH user、host keyが未確認
- alternate SSH / sudo経路がない
- Docker contextがSSH / TCP remote
- 使用予定portを未知processや未知containerが占有
- disk、memory、failed unitの原因が不明

private host名、IP、provider/account実値はbackstage、秘密を含むraw logはGit外へ置く。

## 3. host baseline

新規hostの個人管理者作成、SSH hardening、Docker導入は
[`fresh host bootstrap guide`](fresh-host-bootstrap-guide_ja.md)を使う。既存hostは観測済みの
要件を再利用し、無関係なpackageやserviceを一括削除しない。

管理者をrootful `docker` groupへ暗黙追加しない。Docker mutationは人間のsudo checkpointとし、
agent支援時は[`agent-assisted bootstrap guide`](agent-assisted-bootstrap-guide_ja.md)の境界に従う。

## 4. stack checkoutの検証

target host上の人間管理checkoutを用意する。

```sh
git clone https://github.com/Naohiro2g/mc-remote-stack.git
cd mc-remote-stack
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run mcrctl --help
```

bootstrap期はglobal `mcrctl`やPATH変更を要求しない。以後、checkout rootでは`uv run mcrctl`、
別directoryやsudo checkpointではreview済みcheckoutのexact
`/path/to/mc-remote-stack/.venv/bin/mcrctl`を使う。

## 5. public deployment project

source checkout外に、一環境だけのprojectを作る。

```sh
MC_REMOTE_STACK="$HOME/mc-remote-stack"
MC_REMOTE_PROJECT="$HOME/mc-remote-deployments/official-public-beta"
MC_REMOTE_ARTIFACT_STORE="$HOME/.local/share/mc-remote/artifacts"

cd "$MC_REMOTE_STACK"
uv run mcrctl init "$MC_REMOTE_PROJECT" \
  --format toml \
  --deployment-name official-public-beta \
  --profile vps-server@6 \
  --environment-identity official-public-beta \
  --channel beta \
  --exposure public \
  --purpose integration \
  --preset public-web-paper@2 \
  --artifact-store "$MC_REMOTE_ARTIFACT_STORE" \
  --volume minecraft-data=official-public-beta-minecraft-data \
  --volume caddy-data=official-public-beta-caddy-data \
  --volume caddy-config=official-public-beta-caddy-config \
  --world-identity official-public-beta-world \
  --bind-address 0.0.0.0 \
  --java-port 25565 \
  --mcremote-port 25575
uv run mcrctl validate --project "$MC_REMOTE_PROJECT"
uv run mcrctl repo check --project "$MC_REMOTE_PROJECT"
```

生成された`mc-remote.toml`へ、profileが要求するtyped inputを追加する。

```toml
[[operator_inputs]]
role = "public-routes"
adapter = "public-routes@1"
path = "operator/public-routes/routes.toml"

[[operator_inputs]]
role = "minecraft-server"
adapter = "minecraft-server@1"
path = "operator/minecraft-server/server.toml"
```

`$MC_REMOTE_PROJECT/operator/public-routes/routes.toml`を作成し、DNSで実際に向けるlowercase名を
記録する。これは秘密ではないが環境固有のoperator inputである。

```toml
homepage = "mc-remote.example"
homepage_aliases = ["www.mc-remote.example"]
scratch = "scratch.mc-remote.example"
bridge = "bridge.mc-remote.example"
minecraft = "sb.mc-remote.example"
```

汎用profileはofficial server固有のgameplay / world / performance値を埋め込まない。
`$MC_REMOTE_PROJECT/operator/minecraft-server/server.toml`へ、このrevisionが所有する
typed instance設定を置く。
online mode、secure profile、RCON無効、server port、level identityはこの入力へ委譲せず、
rendererのsecurity invariantとして固定する。

```toml
allow_flight = false
difficulty = "hard"
enable_query = false
enable_status = true
force_gamemode = true
gamemode = "creative"
hardcore = true
log_ips = true
management_server_enabled = false
max_players = 18
max_tick_time = -1
max_world_size = 9984
motd = "McRemote Sandbox Server"
network_compression_threshold = -1
simulation_distance = 6
spawn_protection = 150
view_distance = 10
white_list = false
```

`0.0.0.0`は全interfaceでlistenする明示値であり、公開成功や安全性を単独では意味しない。
provider filter、host firewall、Docker publishの三層を後で照合する。

project rootは最大`0750`、order / lockは最大`0640`とする。world、artifact bytes、backup、
secretをprojectへ入れない。

## 6. EULAとunverified compatibility

EULAは人間が内容を確認してから明示記録する。

```sh
cd "$MC_REMOTE_STACK"
uv run mcrctl accept-eula --project "$MC_REMOTE_PROJECT" --yes
```

`vps-server@6` + `public-web-paper@2`は、public VPSでのb3 live evidenceが正式着地するまでは
`unverified`である。bootstrapを行う人間は`mc-remote.toml`へ次を記録する。

```toml
[acknowledgements]
allow_unverified = true
unverified_reason = "public VPS bootstrap evidence is being established"
allow_eol = false
eol_reason = ""
```

理由を空欄、定型の無意味な文、恒久defaultにしない。正式evidence着地後はcompatibility recordを
別変更で追加し、新規resolveからacknowledgement不要へ移す。

## 7. resolve / plan / artifact / render

```sh
cd "$MC_REMOTE_STACK"
uv run mcrctl resolve \
  --project "$MC_REMOTE_PROJECT" \
  --allow-unverified
uv run mcrctl plan --project "$MC_REMOTE_PROJECT"
uv run mcrctl artifact fetch --project "$MC_REMOTE_PROJECT"
uv run mcrctl render \
  --project "$MC_REMOTE_PROJECT" \
  --output "$MC_REMOTE_PROJECT/generated"
```

unverified警告がある間、`plan`は内容を表示してstatus 1を返す。これはplan内容を捨てる理由でも、
警告を成功扱いで隠す理由でもない。人間は少なくとも次をreviewする。

- profile / presetと各content digest
- lock identity
- Minecraft / protocol version
- OCI digest、Paper / McRemote SHA-256
- public route、public bindとport
- deployment、environment、world、3つのvolume identity
- Caddyがedge / app、Scratch / Bridgeがappだけ、Minecraftがapp / egressに所属すること
- compatibility warningと記録した理由
- canonical generated tree

reviewしたlock identityを人間が別に控える。shellでlockから自動抽出してapplyへ直結しない。

## 8. existing hostのcutover gate

新規hostなら次節へ進む。既存hostではbootstrap用`mcrctl apply`を使わず、専用の
`mcrctl migration auth-enforcement`を使う。Docker / Composeをrunbookから直接操作してこの境界を迂回しない。

特に、既存`vps-server@4` runtimeを停止して同じproject名・volumeのまま`vps-server@5`をbootstrap
しようとすると、lock identityとvolume ownership labelが一致せずfail closedになる。これは
安全機構であり、手動label変更や生成物の上書きで通さない。migrationではroleごとに新しいvolume
identityを明示し、旧runtime停止後に旧volumeからcopyする。旧volumeは成功後も自動削除しない。

最初にread-only planを実行する。

```sh
TARGET_MC_VOLUME="official-public-beta-auth-minecraft-data"
TARGET_CADDY_DATA="official-public-beta-auth-caddy-data"
TARGET_CADDY_CONFIG="official-public-beta-auth-caddy-config"
AUTH_CONFIG_ROOT="$HOME/.config/mc-remote/runtime/official-public-beta/minecraft"

sudo "$MC_REMOTE_STACK/.venv/bin/mcrctl" migration auth-enforcement plan \
  --project "$MC_REMOTE_PROJECT" \
  --output "$MC_REMOTE_PROJECT/generated" \
  --docker-context default \
  --target-volume "minecraft-data=$TARGET_MC_VOLUME" \
  --target-volume "caddy-data=$TARGET_CADDY_DATA" \
  --target-volume "caddy-config=$TARGET_CADDY_CONFIG" \
  --preserve-compose-file "$MC_REMOTE_PROJECT/recovery/compose.recovery-plugins.yaml" \
  --preserve-compose-file "$MC_REMOTE_PROJECT/recovery/compose.homepage.yaml" \
  --auth-config-root "$AUTH_CONFIG_ROOT" \
  --allow-unverified
```

planは旧lock、managed volume、空いている新volume identityを検査し、
`vps-server@5`のtarget lockを一時領域で生成する。project、lock、generated tree、Docker resourceは
変更しない。旧lock作成後に同じrevisionのbundled profileが更新されていても、order、自己検証済みlock、
managed generated bytesが一致し、preset / artifact / operator inputがtargetへそのまま保存される場合だけ
historical sourceとして受理する。`resolve`で稼働中containerと異なるlockへ書き換えない。表示された
source / target lock identityとvolume copyを人間がreviewする。

現行official public betaではrecovery用の追加Compose fileがplugin set、private config、backup mount、
homepageを供給している。このcutoverではそれらを外さず、review済みの追加Composeだけをexact SHA-256で保存し、
transaction内のsnapshotをsource停止とtarget起動の両方に使う。planはlive containerのCompose file列と
working directoryが指定内容に完全一致しなければ拒否する。private configの内容はplanで読み取らず、applyは
source停止後にtarget renderの`auth.enforcement: true`を`$AUTH_CONFIG_ROOT/plugins/McRemote/config.yml`へ
atomicに追加する。既存ファイルが異なる場合は上書きせず停止する。

これは現行サービスを欠落させず認証を修復するための限定的なdeployed-state保存であり、追加plugin / homepageを
preset / lock / renderへ正規化した意味ではない。移行後もdoctorのrender warningは残り得るため、通常の
canonical applyやworld restoreを許可する根拠には使わない。

blockerを修理してplanが通った後だけ、maintenance windowで次を実行する。

```sh
REVIEWED_SOURCE_LOCK="sha256:<planで確認したsource 64-hex>"
REVIEWED_TARGET_LOCK="sha256:<planで確認したtarget 64-hex>"
REVIEWED_PRESERVED_COMPOSITION="sha256:<planで確認したpreserved-composition 64-hex>"

sudo "$MC_REMOTE_STACK/.venv/bin/mcrctl" migration auth-enforcement apply \
  --project "$MC_REMOTE_PROJECT" \
  --output "$MC_REMOTE_PROJECT/generated" \
  --docker-context default \
  --target-volume "minecraft-data=$TARGET_MC_VOLUME" \
  --target-volume "caddy-data=$TARGET_CADDY_DATA" \
  --target-volume "caddy-config=$TARGET_CADDY_CONFIG" \
  --preserve-compose-file "$MC_REMOTE_PROJECT/recovery/compose.recovery-plugins.yaml" \
  --preserve-compose-file "$MC_REMOTE_PROJECT/recovery/compose.homepage.yaml" \
  --auth-config-root "$AUTH_CONFIG_ROOT" \
  --expected-source-lock-identity "$REVIEWED_SOURCE_LOCK" \
  --expected-target-lock-identity "$REVIEWED_TARGET_LOCK" \
  --expected-preserved-composition-identity "$REVIEWED_PRESERVED_COMPOSITION" \
  --allow-unverified \
  --yes
```

transaction stateはproject内の`.mcrctl/migrations/auth-enforcement/state.json`へatomicに記録する。
target起動またはdoctorが失敗しても旧runtimeへ自動復帰しない。停止点と非秘密reasonだけを保持し、
原因を修理して同じ引数・同じ二つのlock identityでapplyを再実行する。再実行は記録済みphaseから
target成功へ進む。state JSONを手編集せず、旧volumeを削除せず、別のbootstrapで上書きしない。

このCLIはunit / deterministic transaction試験まで実装済みである。live適用結果は実施時のdoctor結果と
transaction phaseで記録する。限定的な追加Compose保存をcanonical migration完了と同一視しない。

## 8.1 Scratch runtime configのfail-closed境界

`vps-server@7` / `compose@9`では、Scratchを持つ公開profileの`connection-targets@1`を必須にする。
未指定のorderはresolveで、空配列または`default_sandbox`を含まないlockはrenderで拒否する。beta channelの
`runtime/scratch.json`には非空の`connection_targets`と公開ベータnoticeを必ず出す。

```toml
[[operator_inputs]]
role = "connection-targets"
adapter = "connection-targets@1"
path = "operator/connection-targets/targets.toml"
```

```toml
[[targets]]
id = "beta"
label = "公開ベータ"
sandbox = "sb-beta.mc-remote.com"
```

公開betaのorderはprofileを`vps-server@7`へ進め、上記入力を追加してからresolve / renderする。
同じCompose file集合でruntimeを再作成した後、doctorは
`https://<scratch route>/mc-remote-runtime-config.json`を取得し、canonical renderとの完全一致、
非空target、defaultの包含、beta noticeを確認する。成功時は`OK doctor scratch-runtime=current`を出す。
`default_sandbox`だけを配信してScratch内蔵stable fallbackへ委ねる構成は正常系にしない。

## 9. bootstrap apply

管理者がrootful Dockerを直接使えないbaselineでは、人間がreview済みcheckoutとprojectを
exact pathで指定してsudo実行する。agentが書込み可能なcheckout、venv、generated outputを
sudoで実行しない。

```sh
MC_REMOTE_STACK="$HOME/mc-remote-stack"
MC_REMOTE_PROJECT="$HOME/mc-remote-deployments/official-public-beta"
REVIEWED_LOCK_IDENTITY="sha256:<planで確認した64-hex>"

sudo "$MC_REMOTE_STACK/.venv/bin/mcrctl" apply \
  --project "$MC_REMOTE_PROJECT" \
  --output "$MC_REMOTE_PROJECT/generated" \
  --expected-lock-identity "$REVIEWED_LOCK_IDENTITY" \
  --docker-context default \
  --bootstrap \
  --yes \
  --allow-unverified
```

applyはDocker / Compose、canonical render、current lock、port、container、volumeを再検証する。
失敗時は新containerをdownするがmanaged world volumeを削除しない。自動rollbackの失敗は
`apply_rollback_failed`として停止し、成功を主張しない。

長時間無表示にしないため、applyは秘密を含まない固定stepだけを逐次表示する。
`verify-render`、`validate-lock`、`docker-preflight`、`runtime-preflight`、
`check-ports`、`pull-images`、`prepare-volumes`、
`start-services-and-wait timeout=<seconds>`、`post-check`、`complete`が通常経路である。
失敗時は必要に応じて`rollback-containers`を表示する。Docker log、registry応答、
container環境値をprogressへ混ぜない。失敗detailはstderr（空ならstdout）の最後の非空行を
制御文字除去・秘密値mask・長さ制限したものだけであり、完全なDocker出力ではない。

## 10. doctorとpublic reachability

対象host上のread-only doctor:

```sh
sudo "$MC_REMOTE_STACK/.venv/bin/mcrctl" doctor \
  --project "$MC_REMOTE_PROJECT" \
  --output "$MC_REMOTE_PROJECT/generated" \
  --docker-context default
```

期待値:

- `runtime=healthy`
- canonicalな通常起動では`render=current`
- recovery override等の追加Compose fileが残る移行中runtimeでは
  `WARN render=additional-compose-files`。health / protocol確認は有効だが、canonical化完了とは
  扱わない
- `network=public`
- Caddy 80 / 443、Minecraft TCP/UDP 25565、McRemote 25575がlockと一致
- Scratch / Bridgeにはhost publishがなく、app networkは`internal=true`かつIPv6無効
- Minecraftのdefault gatewayはIPv6無効のegressで、Mojang初回取得が成功する
- token無しhelloが明示的`auth_required`。成功時は`doctor_auth_not_enforced`でFAIL
- compatibilityは正式evidence着地まで`unverified` warning

別networkから、provider filter、host firewall、Docker publishを通したJava / McRemote
reachabilityを確認する。target host自身からpublic IPへ接続できることだけで外部到達を代用しない。
token、pair code、player UUIDをtranscriptへ保存しない。

新transactionのdoctorと外部到達がPASSした後、現行hostに残るstaging用UFW ruleを人間が
番号と内容で再確認して削除する。provider filter側の25566 tcp/udp、25576 tcpも同じcheckpointで
削除する。稼働portのruleと`MC_REMOTE_INGRESS` chainは一括flushしない。

```sh
sudo ufw status numbered
sudo ufw delete allow 25566/tcp
sudo ufw delete allow 25566/udp
sudo ufw delete allow 25576/tcp
sudo ufw status verbose
sudo iptables -S DOCKER-USER
sudo iptables -S MC_REMOTE_INGRESS
```

## 11. restartabilityと証跡

同じorder / lock / generated treeでdoctorを再実行し、作業を中断しても再開できるhandoffを残す。

```markdown
- target: backstage上の参照
- checkout commit:
- project path:
- generated path:
- reviewed lock identity:
- last PASS:
- expected warning:
- host mutation:
- public reachability:
- next human checkpoint:
```

このbootstrapの正式なrelease / protocol根拠に使う回は`live-auto` transcriptをsanitizedし、
knowledge ownerへevidence draftとしてhandoffする。rawはGit外、private inventoryはbackstage、
公開可能なrunbookと実装は本repoへ置く。

## 12. 残る完成度phase

Caddy、Scratch、Bridgeのcore transactionは`vps-server@5`へ取り込んだ。過去の6GB official
VPSで実証したTLS / WSS / rollbackを現行SSOTの自動claimとして完成させる残作業は次である。

1. homepageのcontent-addressed artifactとprovenance
2. backup retention / off-host live smoke / rollback cleanup contract
3. HTTPS / WSS / Bridge→Minecraft smokeのdoctor claim
4. stable / betaの排他切替、upgrade、同一hash redeploy
5. provider filter / UFW / `DOCKER-USER`観測のsanitized evidence schema

2026-07-26の6GB実機read-only再確認では、現行legacy Composeに`mc-remote_edge` /
`mc-remote_app` networkがあり、named volumeはなくruntime stateをhost bind directoryで
保持していた。UFWには80 / 443 / 25565 tcp+udp / 25575に加え、現行の排他stable / betaでは
使わない旧staging用25566 tcp+udp / 25576 tcpが残っていた。これらを新profileの既定値へ
転記しない。provider filter、`DOCKER-USER`からjumpするproject固有chain、実listenを照合し、
desired lockにないruleを人間review後にcleanupする。

同hostのIPv4 `MC_REMOTE_INGRESS`はRELATED / ESTABLISHED、TCP 80 / 443 / 25565 / 25575、
UDP 19132を許可して残りをDROPしていた。Bedrockのpublic host portはUDP 25565だが、Composeが
container UDP 19132へDNATし、`DOCKER-USER` hookではcontainer側portを照合するため、この二つを
port driftと誤判定しない。一方、IPv6 `DOCKER-USER`には同等chainが無かった。global IPv6、
provider IPv6 filter、DNS AAAA、Docker IPv6 publishのいずれかが有効なら非対称な公開面になる。
Phase 2は「IPv6を使わずAAAAも無い」または「IPv4と同等の明示filterを持つ」のどちらかを
plan / doctorで確認し、未判定のままpublic readinessを主張しない。

backup / restoreの決定論的CLIには、age暗号文とtransfer record sidecarのoff-host転送、
明示的なremote list / record download / ciphertext download / decrypt、およびcurrent
lockへ束縛したworld-only `plan` / `apply`がある。world restoreはarchive SHA-256、CRC、
entry安全性、managed volume、current containerを検証し、plugin dataやcredentialを
展開しない。起動またはdoctor失敗時は旧worldへrollbackし、成功時も旧worldを保持する。
残るphase 2は、実FTPS相手の往復smoke、retention値、成功後rollback directoryの削除時点を
人間review込みで確定することである。

2026-07-26時点で、official homepage / Scratch stable・beta / Bridge stable・beta /
Minecraft stable・betaの公開名にはAAAA RRが無いことをRR type指定で確認した。CNAMEだけの
応答やIPv4-mapped addressをAAAAと誤判定しない。これはDNSの時点観測であり、hostのglobal
IPv6やDocker IPv6設定の確認を省略する理由にはしない。

同日のhost確認では、providerがIPv6 serviceを提供せず、global IPv6 addressは無く、default /
project `edge` / project `app`の全Docker networkでIPv6が無効だった。現行Composeの短縮port
syntaxは`docker ps`へ`[::]` publishも表示するが、provider / host / Docker network / DNSの
いずれにも外部IPv6経路は成立していない。新rendererはこの偶然へ依存せず、公開host bindを
IPv4として明示する。

現行稼働serviceはCaddy、Scratch stable・beta、Bridge stable・beta、profile起動した
Minecraft betaである。Minecraft betaだけにcontainer healthがあり、Web / Bridgeはrunning
だけだった。Phase 2ではprocess runningをservice readinessと同一視せず、Caddy config /
HTTPS、Scratch runtime、Bridge WSSまたは内部healthをdoctorの個別claimとして検証する。

現行`app` networkは`internal=false`である。これは移行中のstable BridgeがVPS外のbackendへ
到達する構成を含むためで、恒久的に全backendへ無制限egressを与える根拠にはしない。新profileは
Bridge routeがproject内だけで閉じる場合と、明示した外部backendを必要とする場合を区別し、
network isolation / egressをplanへ表示する。実機初回起動ではPaperが
`piston-data.mojang.com`からruntimeデータを取得するため、Minecraftまでinternal appだけへ
閉じると`UnknownHostException`でrestart loopになった。このためScratch / Bridgeのapp isolationは
維持し、Minecraftだけを`gw_priority`付きegressへ接続する。Compose 2.33.1以上を前提とし、
default gatewayを暗黙のnetwork接続順へ依存させない。

過去の実証済み構造を捨ててWeb面を新規発明しない。archiveの観測を現行contractとtestへ
再著作し、残るclaimもpublic読者がarchiveなしで完結できる状態へ移す。
