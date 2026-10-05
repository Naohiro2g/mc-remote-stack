# ホーム用ケータリング手順

公開済み b7.post2 を新しいホームセットへ配置する手順です。`home-alpha-full@2` は Caddy、Scratch、Bridge、Paper＋McRemote の4サービスを同じ host に配置します。設定生成と doctor は Scratch の正式な runtime-config schema を使います。

この構成は Java 版のゲーム接続を扱います。HTTPS／WSS の終端は host 側に用意します。Tailscale Serve など、信頼済み証明書で host の loopback HTTP へ転送する入口を使えます。公開 DNS、ACME、WireScope 配信、追加 Minecraft plugins、backup を含む VPS 構成は[公開VPS手順](public-vps-bootstrap-guide_ja.md)で扱います。

## 1. host と入口を確認する

運用者自身の private ops 情報から、次を揃えます。公式運用では Backstage を使います。

| 値 | 用途 |
| --- | --- |
| host と Stack checkout | 全操作を実行するマシンと、対応コードの版 |
| deployment 名と order の置き場所 | 今回のセットを他のセットから識別する |
| Scratch HTTPS URL / Bridge WSS URL | browser が使う入口 |
| Minecraft hostname と Java TCP 25565 への経路 | ゲームからの接続と、Scratch の target |

Ubuntu の operator 環境は[host 準備手順](fresh-host-bootstrap-guide_ja.md)で用意します。対象 host の通常の管理ユーザーで確認します。

```sh
~/mc-remote-stack/tools/bootstrap-ubuntu-operator.sh --check
```

この preset は host の `127.0.0.1` に Java TCP 25565、McRemote TCP 25575、Caddy TCP 8443・8444 を配置します。各 port の listener と所有するプロセスを確認します。既存セットと衝突する場合は、そのセットの扱いを運用者と決めます。

Scratch の入口は loopback 8443、Bridge の入口は loopback 8444 へ転送します。Java の入口は loopback 25565 へ転送します。これらの host 転送と利用端末からの到達は運用者が用意します。Bridge から McRemote へは Compose 内の TCP 25575 で接続します。

## 2. order を用意して設定を確認する

次は例です。URL、hostname、deployment 名を自分の構成へ置き換えます。operator Notice を付ける場合は `[[notices]]` を一件追加できます。

文面の編集・削除と一時ファイルからの収容は[Scratchのお知らせ編集](scratch-notice-guide_ja.md#4-ホーム用のコンパクトorderを使う場合)を参照します。製品noticeは別に表示されるため、起動後に両方を確認します。

```toml
schema_version = 1
deployment = "home-trial"
preset = "home-alpha-full@2"

[surfaces]
scratch_url = "https://home.example.org:8443/"
bridge_url = "wss://home.example.org:8444/"

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
| `preview/Caddyfile` | Scratch / Bridge の HTTP 転送先 |
| `preview/runtime/scratch.json` | `schema_version: 1`、target、Bridge URL。正式 schema の検査は生成時に行う |
| `preview/runtime/minecraft/plugins/McRemote/config.yml` | 認証強制と session-only store の初回設定 |

dry-run はファイルを生成するだけです。artifact の取得・実物の SHA 検査、Docker 起動、port の空き、HTTPS の到達を確認した結果ではありません。

## 3. 起動して配信結果を確認する

構成と host 転送を確認し、Minecraft EULA の受諾を含む起動の準備ができたら進めます。この構成の apply は Minecraft の EULA を受諾する設定で起動します。

```sh
mcrctl apply ./mc-remote.toml
mcrctl doctor home-trial
```

apply は `prepare → preflight → render → artifacts → compose-check → pull → start → record` を表示します。各表示はその操作に入る時点を示します。`OK apply` は Compose の起動完了、`OK doctor` は exact image、container 稼働、port、配信 runtime schema、Bridge target と McRemote 到達、認証強制の検査成功です。

world と credential state は `<deployment>-minecraft-data`、Caddy state は `<deployment>-caddy-data` と `<deployment>-caddy-config` に保持します。world directory は `<deployment>-world` です。通常 apply は同じ deployment の停止済み container があっても既存 world の更新として扱います。生成した確認用 directory は起動時には使わず、実際の配置は Stack の state directory に保存します。

browser で Scratch の target と運用者 Notice を確認し、Minecraft client で接続します。pairing とブロック操作は別に確認します。b7.post2 の Scratch は設定不正でも Editor を開きますが、画面の接続無効案内だけでは意図的な無効設定と区別できません。設定不正は browser console の warning と doctor の配信設定検査で確認します。doctor の成功だけでは人間が行うこれらの操作まで確認したことにはなりません。

失敗した場合は最後の進行段階、`FAIL` の reason、表示された Docker のエラーから確認します。port 衝突は listener の所有者、artifact 取得失敗は取得先と SHA、起動失敗は lock が指す Compose と container log、doctor の runtime 不一致は order と実際に配信される設定を照合します。修正後の再実行範囲は、残った container・volume・current state を観測して決めます。
