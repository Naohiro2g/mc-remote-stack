# ホーム用ケータリング手順

公開済み b7.post2 をホームセットへ配置する手順です。`home-alpha-full@4` は Caddy、Scratch、Bridge、Paper＋McRemote の4サービスを同じ host に配置し、CaddyからWireScopeも配信します。MinecraftにはLuckPerms、Geyser、Floodgate、ViaVersion、ViaBackwardsを配置します。

Java版はTCP、統合版はUDPで接続します。HTTPS／WSS の終端は host 側に用意します。Tailscale Serve など、信頼済み証明書で host の loopback HTTP へ転送する入口を使えます。公開 DNS、ACME、backup を含む VPS 構成は[公開VPS手順](public-vps-bootstrap-guide_ja.md)で扱います。

## 作業を始める前に共有すること

この手順のコマンドは、配置先hostのStack checkoutで、通常の管理ユーザーとして実行します。ラップトップから操作する場合は、対象hostへSSH接続してから進めます。hostの初期化や旧セットの削除は、セットを配置する操作とは別に、そのhostの状態から判断します。

配置先・セット・入口を、作業者と運用者で次のように確認します。公式運用ではBackstage、OSS利用者は自分のprivate ops情報を使います。

| 確認すること | この手順での意味 |
| --- | --- |
| どのhostに何を配置するか | この例はホーム用の公開済みb7.post2。進行中のrelease candidateを選ぶ操作ではない |
| 新規か、どのdeploymentの更新か | 同じdeployment名は既存world・plugin設定・認証データを引き継ぐ。別セットは別名で識別する |
| 既存セットをどう扱うか | 起動中のサービス、使用port、残すvolumeを確認する。停止でportが空いた場合は、その結果を記録する |
| 誰がどこから使うか | Tailscaleなら利用端末もtailnetへ接続する。LANや外部公開では、そのネットワークからの入口を用意する |
| 人間が確認する操作 | 対象のScratchを開き、実接続先、Java／統合版参加、権限付与後のhelloと操作結果を確認する |

各段階で、実行したhost・deployment・preset、最後の進行表示と結果をprivate opsへ残します。別のScratchや別サーバーでの成功を今回の結果へ混ぜないよう、画面の設定先と実接続先も確認します。

## 1. host と入口を確認する

運用者自身の private ops 情報から、次を揃えます。公式運用では Backstage を使います。

| 値 | 用途 |
| --- | --- |
| host と Stack checkout | 全操作を実行するマシンと、対応コードの版 |
| deployment 名と order の置き場所 | 今回のセットを他のセットから識別する |
| Scratch HTTPS URL / Bridge WSS URL / WireScope HTTPS URL | browserが使う入口。ScratchとWireScopeは別originにする |
| Minecraft hostname と Java TCP 25565 への経路 | ゲームからの接続と、Scratch の target |
| 統合版を受け付けるhostのIPv4とUDP 19132への経路 | TailscaleのIP、LANのIP等、利用端末から届く入口 |

Ubuntu の operator 環境は[host 準備手順](fresh-host-bootstrap-guide_ja.md)で用意します。対象 host の通常の管理ユーザーで確認します。

```sh
~/mc-remote-stack/tools/bootstrap-ubuntu-operator.sh --check
```

この preset は host の `127.0.0.1` に Java TCP 25565、McRemote TCP 25575、Caddy TCP 8443・8444・8445 を配置します。各 port の listener と所有するプロセスを確認します。既存セットと衝突する場合は、そのセットの扱いを運用者と決めます。

統合版は、orderで選んだhostのIPv4にUDP 19132を配置します。Tailscale経由で使う場合はhostの`tailscale ip -4`で表示されるIPv4を使います。Tailscale ServeのTCP転送やHTTPS proxyは、このUDP入口の代わりにはなりません。接続する端末のtailnet参加と、hostのfirewallも確認します。

Scratch の入口は loopback 8443、Bridge の入口は loopback 8444、WireScopeの入口はloopback 8445へ転送します。Java の入口は loopback 25565 へ転送します。これらの host 転送と利用端末からの到達は運用者が用意します。Bridge から McRemote へは Compose 内の TCP 25575 で接続します。

## 2. order を用意して設定を確認する

次は例です。URL、hostname、deployment 名を自分の構成へ置き換えます。operator Notice を付ける場合は `[[notices]]` を一件追加できます。

```toml
schema_version = 1
deployment = "home-trial"
preset = "home-alpha-full@4"

[surfaces]
scratch_url = "https://home.example.org:8443/"
bridge_url = "wss://home.example.org:8444/"
wirescope_url = "https://home.example.org:8445/"
bedrock_bind_address = "192.0.2.30"

[[targets]]
id = "trial"
label = "Home trial"
sandbox = "home.example.org"
default = true
```

`mc-remote.toml` に保存したら、空の確認用 directory を指定します。

`bedrock_bind_address`は、このhostが実際に持つIPv4へ置き換えます。上の`192.0.2.30`は説明用です。Docker内のGeyserはUDP 19132で待ち受け、Dockerが選んだhostのIPv4へ公開します。

```sh
mcrctl apply ./mc-remote.toml --dry-run --output ./preview
```

`PLAN apply` が出たら、次の生成物を確認します。生成物を直す場合は元の order を編集し、別の空 directory へ再生成します。

| 生成物 | 確認する内容 |
| --- | --- |
| `preview/mc-remote.lock.json` | preset、exact artifact、Scratch contract、target、入口の URL |
| `preview/compose.yaml` | 4サービス、loopbackのTCP port、選んだIPv4のUDP 19132、3 named volumes、5つの標準pluginのmount、Minecraftのinternet egress |
| `preview/Caddyfile` | Scratch / BridgeのHTTP転送、WireScopeの静的配信とhandoff用header |
| `preview/runtime/scratch.json` | `schema_version: 1`、target、Bridge / WireScope URL。正式 schema の検査は生成時に行う |
| `preview/runtime/minecraft/plugins/McRemote/config.yml` | 認証強制と session-only store の初回設定 |

dry-run はファイルを生成するだけです。WireScope本体の展開、artifactの取得・実物のSHA検査、Docker起動、port の空き、HTTPS の到達を確認した結果ではありません。

## 3. 起動して配信結果を確認する

構成と host 転送を確認し、Minecraft EULA の受諾を含む起動の準備ができたら進めます。この構成の apply は Minecraft の EULA を受諾する設定で起動します。

```sh
mcrctl apply ./mc-remote.toml
mcrctl doctor home-trial
```

apply は `prepare → preflight → render → artifacts → wirescope → compose-check → pull → start → verify → record` を表示します。各表示はその操作に入る時点を示します。`verify` は起動後の4サービスを再取得し、再起動中でないこと、exact image、port、Minecraft healthを確認します。`OK apply` はこの確認とcurrent記録の完了、`OK doctor` は配信 runtime schema、Bridge target と McRemote 到達、認証強制を含む検査成功です。`wirescope`段階でZIPとmanifest・全assetを照合し、Caddyへ配置します。doctorの`wirescope=current`は、配信されたindexのSHAとScratch／WireScope双方のhandoff用headerを確認した結果です。

`verify`とdoctorは、Minecraftの`/data/plugins/`に配置された5つの標準JARのSHAをlockと照合します。`plugins=current`はこの配置照合の結果です。各pluginの起動、アカウントへの権限付与、ゲーム端末からの参加は、続く操作で確認します。LuckPermsは初回起動時に依存ライブラリを取得するため、Minecraftのinternet egressも使います。

WireScope asset、Scratch runtime JSONとCaddy設定はコンテナから読める `0644` で生成します。lock／currentなどは `0600` で保持します。同じ内容のファイルはinodeを保ち、必要なら権限を補正するため、既存のbind mountにも補正が届きます。`deployment_runtime_restarting` が出た場合は、そのサービスのlogで起動失敗の原因を確認します。

world と credential state は `<deployment>-minecraft-data`、Caddy state は `<deployment>-caddy-data` と `<deployment>-caddy-config` に保持します。world directory は `<deployment>-world` です。通常 apply は同じ deployment の停止済み container があっても既存 world の更新として扱います。生成した確認用 directory は起動時には使わず、実際の配置は Stack の state directory に保存します。

browserでScratchのtargetと運用者Noticeを確認します。b7.post2 の Scratch は設定不正でも Editor を開きますが、画面の接続無効案内だけでは意図的な無効設定と区別できません。設定不正は browser console の warning と doctor の配信設定検査で確認します。doctor は browser から Bridge への WSS upgrade を直接検査しません。

失敗した場合は最後の進行段階、`FAIL` の reason、表示された Docker のエラーから確認します。port 衝突は listener の所有者、artifact 取得失敗は取得先と SHA、起動失敗は lock が指す Compose と container log、doctor の runtime 不一致は order と実際に配信される設定を照合します。修正後の再実行範囲は、残った container・volume・current state を観測して決めます。

## b7.post2の認証初期化

b7.post2の新セットは認証データの明示初期化が必要です。Dockerのサービスがhealthyでも、認証データが未初期化ならtokenを発行できません。Minecraftのペアリングコマンドが成功を表示しても、Scratch側で `credential_store_unavailable`（`operation: issue`）になる場合があります。

対象deploymentのMinecraftコンテナへ、通常の管理ユーザーで状態確認コマンドを送ります。応答はコンテナのlogで確認します。次の`home-trial`は自分のdeployment名へ置き換えます。

```sh
docker exec --user 1000:1000 home-trial-minecraft-1 mc-send-to-console "mcremote credential status"
docker logs --since 1m home-trial-minecraft-1
```

状態が `UNINITIALIZED / Explicit credential bootstrap is required` なら、今回用意した新セットであることと認証データが未作成であることを確認し、この版が提供する初期化を実行します。

```sh
docker exec --user 1000:1000 home-trial-minecraft-1 mc-send-to-console "mcremote credential bootstrap"
docker exec --user 1000:1000 home-trial-minecraft-1 mc-send-to-console "mcremote credential status"
docker logs --since 1m home-trial-minecraft-1
```

`Credential domain bootstrapped` と `HEALTHY` を確認してから、Scratchで新しいコードを取得し、ペアリングします。初期化結果が正常でなければ、その応答と保存先の状態を調べます。通常doctorの `auth=enforced` はtokenなしhelloの拒否を確認するもので、credential domainのhealth確認はこの状態表示で行います。

## 4. pluginの起動とアカウント権限を確認する

対象コンテナの起動logで、5つの標準pluginが起動したこと、McRemoteが`LuckPermsPermissionManager`を選び、`Successfully connected to LuckPerms.`を出していることを確認します。JARの配置やMinecraftのhealthy表示とは別の確認です。

Geyserの設定は`/data/plugins/Geyser-Spigot/config.yml`、LuckPermsの設定とDBは`/data/plugins/LuckPerms/`に保存されます。これらはMinecraftのdata volumeに属します。GeyserのUDP portが19132、Java接続の認証方式がFloodgateになっていることを確認します。設定の見方は[Geyser](https://geysermc.org/wiki/geyser/setup/)と[Floodgate](https://geysermc.org/wiki/floodgate/setup/)の公式手順も参照できます。

このpresetのGeyserは初回起動で`java.auth-type: online`の設定を作成します。`online`はJavaアカウントで認証する方式です。Floodgateで統合版アカウントを受け入れるため、元の設定を保存して`floodgate`へ変更します。次は初期設定の`  auth-type: online`が一箇所ある場合の操作です。既に編集した設定では、現在の内容と保存済みの設定を確認してから変更します。

```sh
docker exec --user 1000:1000 home-trial-minecraft-1 cp /data/plugins/Geyser-Spigot/config.yml /data/plugins/Geyser-Spigot/config.before-floodgate.yml
docker exec --user 1000:1000 home-trial-minecraft-1 sed -i 's/^  auth-type: online$/  auth-type: floodgate/' /data/plugins/Geyser-Spigot/config.yml
docker restart home-trial-minecraft-1
mcrctl apply ./mc-remote.toml
mcrctl doctor home-trial
```

再起動後のapplyは同じorderを使い、起動と配置の確認を待ちます。起動logとGeyserの設定でFloodgateを使う状態になったことを確認します。

新しいLuckPermsには、このアカウントのMcRemote権限はまだ付与していません。既存tokenを持っていても、必要なpermissionがなければ再接続時のhelloは`permission_denied`になります。未付与時の拒否を確認してから、対象アカウントと付与内容を運用者と確認します。

tokenの寿命が切れている場合は先に`token_expired`になります。その場合は新しくペアリングし、その後のhelloで権限を確認します。認証tokenの取得と、LuckPermsによる操作許可は別の段階です。

次は、検証アカウントにonline・offlineの両方と範囲1000を付ける例です。`PLAYER_UUID`を対象のUUID、`home-trial`をdeployment名へ置き換えます。Minecraftへの参加中だけ使う場合はonline、未参加でも継続稼働する作品にはofflineを選びます。二つは独立した権限です。

```sh
docker exec --user 1000:1000 home-trial-minecraft-1 mc-send-to-console "lp user PLAYER_UUID permission set mcr.online true server=global"
docker exec --user 1000:1000 home-trial-minecraft-1 mc-send-to-console "lp user PLAYER_UUID permission set mcr.offline true server=global"
docker exec --user 1000:1000 home-trial-minecraft-1 mc-send-to-console "lp user PLAYER_UUID meta set mcr.build.range 1000 server=global"
docker exec --user 1000:1000 home-trial-minecraft-1 mc-send-to-console "lp user PLAYER_UUID permission info"
docker exec --user 1000:1000 home-trial-minecraft-1 mc-send-to-console "lp user PLAYER_UUID meta info"
docker logs --since 5m home-trial-minecraft-1
```

コンソールへの送信だけでなく、logで各操作の応答を確認します。アカウントとgroupの継承を含む権限設定は運用者の管理情報です。McRemoteは`server=global` contextのeffective permissionとmetaをhello時に解決します。付与・変更後はScratchを再接続し、helloのpermissionsが指定した内容になったことを確認します。

## 5. 接続と作品の操作を確認して、結果を残す

| 確認 | 人間が見る結果 |
| --- | --- |
| Java版で参加 | 今回のworldへ入り、ゲームモードと既存worldが想定どおりである |
| 統合版で参加 | 選んだhostのIPv4・port 19132から同じworldへ入れる。端末とclient版も記録する |
| Scratchで接続 | WireScope miniの設定先・実接続先が今回のtargetに一致し、helloのpermissionとrangeが意図どおりである |
| chat.post | 送信した文字列がresultとして返り、WireScopeにも往復が見える |
| block write/read | 意図した位置へ書き込み、同じ位置を読み戻して結果を確認する |
| 未参加での操作 | offlineを付与した用途では、ゲームから退出して再接続し、意図した操作が通る |

ViaVersion/ViaBackwardsの起動は、その場で全client版の接続を確認したという意味ではありません。今回参加したclientの版と結果を残します。すべての人間操作が終わる前に記録を引き継ぐ場合は、確認済みと残る操作を分けて記載します。

次の内容をprivate opsへ引き継ぐと、後続セッションでも会話の記憶に頼らず再開できます。

- 作業host、Stackの版、deployment、orderの場所、preset、currentが指すlock。
- 各入口と利用端末からの到達方法。sudoで行ったhost転送・firewall操作があれば、その操作と結果。
- 既存セットをどう扱い、どのworld・volume・plugin設定・認証データを保持したか。
- 対象アカウントへ付与したpermission・range、再接続後のhelloと実操作結果。
- 最後に成功した段階、未確認の操作、失敗した場合のreasonと次に調べる対象。

実際のhost・URL・UUID等は運用者の管理情報へ置きます。公開手順は上の置換用の例を使います。
