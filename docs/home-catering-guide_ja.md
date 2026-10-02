# ホーム用ケータリング手順

公開済み b7.post2 を新しいホームセットへ配置する手順です。`home-alpha-full@3` は Caddy、Scratch、Bridge、Paper＋McRemote の4サービスを同じ host に配置し、CaddyからWireScopeも配信します。設定生成と doctor は Scratch の正式な runtime-config schema を使います。

この構成は Java 版のゲーム接続を扱います。HTTPS／WSS の終端は host 側に用意します。Tailscale Serve など、信頼済み証明書で host の loopback HTTP へ転送する入口を使えます。公開 DNS、ACME、追加 Minecraft plugins、backup を含む VPS 構成は[公開VPS手順](public-vps-bootstrap-guide_ja.md)で扱います。

## 1. host と入口を確認する

運用者自身の private ops 情報から、次を揃えます。公式運用では Backstage を使います。

| 値 | 用途 |
| --- | --- |
| host と Stack checkout | 全操作を実行するマシンと、対応コードの版 |
| deployment 名と order の置き場所 | 今回のセットを他のセットから識別する |
| Scratch HTTPS URL / Bridge WSS URL / WireScope HTTPS URL | browserが使う入口。ScratchとWireScopeは別originにする |
| Minecraft hostname と Java TCP 25565 への経路 | ゲームからの接続と、Scratch の target |

Ubuntu の operator 環境は[host 準備手順](fresh-host-bootstrap-guide_ja.md)で用意します。対象 host の通常の管理ユーザーで確認します。

```sh
~/mc-remote-stack/tools/bootstrap-ubuntu-operator.sh --check
```

この preset は host の `127.0.0.1` に Java TCP 25565、McRemote TCP 25575、Caddy TCP 8443・8444・8445 を配置します。各 port の listener と所有するプロセスを確認します。既存セットと衝突する場合は、そのセットの扱いを運用者と決めます。

Scratch の入口は loopback 8443、Bridge の入口は loopback 8444、WireScopeの入口はloopback 8445へ転送します。Java の入口は loopback 25565 へ転送します。これらの host 転送と利用端末からの到達は運用者が用意します。Bridge から McRemote へは Compose 内の TCP 25575 で接続します。

## 2. order を用意して設定を確認する

次は例です。URL、hostname、deployment 名を自分の構成へ置き換えます。operator Notice を付ける場合は `[[notices]]` を一件追加できます。

```toml
schema_version = 1
deployment = "home-trial"
preset = "home-alpha-full@3"

[surfaces]
scratch_url = "https://home.example.org:8443/"
bridge_url = "wss://home.example.org:8444/"
wirescope_url = "https://home.example.org:8445/"

[[targets]]
id = "trial"
label = "Home trial"
sandbox = "home.example.org"
default = true
```

`mc-remote.toml` に保存したら、空の確認用 directory を指定します。

```sh
mcrctl apply ./mc-remote.toml --dry-run --output ./preview
```

`PLAN apply` が出たら、次の生成物を確認します。生成物を直す場合は元の order を編集し、別の空 directory へ再生成します。

| 生成物 | 確認する内容 |
| --- | --- |
| `preview/mc-remote.lock.json` | preset、exact artifact、Scratch contract、target、入口の URL |
| `preview/compose.yaml` | 4サービス、loopback の port、3 named volumes、Minecraft の internet egress |
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

WireScope asset、Scratch runtime JSONとCaddy設定はコンテナから読める `0644` で生成します。lock／currentなどは `0600` で保持します。同じ内容のファイルはinodeを保ち、必要なら権限を補正するため、既存のbind mountにも補正が届きます。`deployment_runtime_restarting` が出た場合は、そのサービスのlogで起動失敗の原因を確認します。

world と credential state は `<deployment>-minecraft-data`、Caddy state は `<deployment>-caddy-data` と `<deployment>-caddy-config` に保持します。world directory は `<deployment>-world` です。通常 apply は同じ deployment の停止済み container があっても既存 world の更新として扱います。生成した確認用 directory は起動時には使わず、実際の配置は Stack の state directory に保存します。

browserでScratchのtargetと運用者Noticeを確認し、Minecraft clientで接続します。WireScope miniから本体を開き、観測データが届くことを確認します。pairing とブロック操作は別に確認します。b7.post2 の Scratch は設定不正でも Editor を開きますが、画面の接続無効案内だけでは意図的な無効設定と区別できません。設定不正は browser console の warning と doctor の配信設定検査で確認します。doctor は browser から Bridge への WSS upgrade を直接検査しません。doctor の成功だけでは人間が行うこれらの操作まで確認したことにはなりません。

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
