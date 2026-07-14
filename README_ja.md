# mc-remote-stack

[English here.](README.md)

`mc-remote-stack` は、McRemote（マイクラリモコン）サーバーを再現可能な形で設置・運用するためのパッケージ。Scratchクライアントを含む。手書き編集した `mc-remote.yml` から、検証済みでdigestを固定したruntime設定を生成する。レンタルVPSサーバー（XServer）を想定。

このプロジェクトは、次のものとは意図的に分離している。

- `mc-remote-knowledge`: ナレッジベースのリポ。アーキテクチャと意思決定のSSOT（Single Source of Truth、信頼できる唯一の情報源）（公開予定、現在は未公開）
- `server-runbook`: 新しいマシンの立ち上げと運用知識のリポ。（公開予定、現在は未公開）
- deployment project: instance固有のdesired state（望ましい状態）とlockデータ

## 何を動かすパッケージか

```text
McRemote server package
├─ caddy            HTTPS/WSSの入口・静的コンテンツの配信ウェブサーバー
├─ homepage         システム紹介用の静的ホームページ（例としてmc-remote.com）
├─ scratch-editor   stable/dev/showcase　マイクラリモコンのScratchクライアント
├─ bridge           WSS⇔TCP・接続先のallowlist　　Scratchとマイクラリモコンを中継
├─ minecraft        Paper/NeoForge + McRemote　　マイクラサーバーたち
├─ deploy           検証・適用・更新・ロールバック
├─ backup           バックアップ・外部転送
└─ monitoring       health・容量・backup鮮度・doctor
```

CaddyがWeb側の共通入口となり、システム紹介用のホームページを配信、ScratchからBridgeのWSS接続に中継。CaddyはGo言語で開発されたオープンソースのWebサーバーソフトウェアで、「自動HTTPS（証明書の自動発行・自動更新）機能」を標準搭載している。[caddyserver/caddy](https://github.com/caddyserver/caddy)

Bridgeは、ScratchのWS通信をTCPソケット通信（マイクラサーバーにロードされたマイクラリモコンプラグイン＝ソケットサーバー）へ接続する。マイクラサーバーは同じVPSへ置くことも、XServer GAMEsやホームサーバーなど別のマシンへ置くことも可能。配置先が違っても同じarchitectureで、profileとinstance固有の設定でmoduleを組み合わせて使用可能。

## 開発準備

```sh
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run mcrctl --help
```

## 最初の垂直スライス（機能縦割り）

```sh
uv run mcrctl init ./deployment --profile official-vps
uv run mcrctl validate --project ./deployment
uv run mcrctl repo check --project ./deployment
uv run mcrctl plan --project ./deployment
uv run mcrctl accept-eula --project ./deployment --yes
uv run mcrctl render --project ./deployment --output ./deployment/generated
```

`plan` は、EULAへの同意と変更不能なartifact identityがそろうまで停止。対象にはOCI image、Paper、plugin JARに加え、ホームページのversionとarchive SHA-256も含まれる。未解決のselectorを暗黙に本番deploymentへ変換することはない。
`render` は同じgateを通過した後にだけ、Compose、Caddy、Scratch runtime、Bridge route、ServerBackup（Paperプラグイン）の設定を生成。
この最初のvertical sliceには、生成したファイルをhostへ適用する機能はまだ実装されていない。

初期化したlockは、意図的に特定versionへ固定していない。profileが選ぶものはトポロジーとポリシーであり、マイクラやマイクラリモコンのバージョンではない。このため既存サーバーを移行するときは、回収した現物ファイル（バージョン）を固定するため、インフラ移行と同時にMcRemoteのupgradeを強制されずに済む。

### 同じVPSへ開発サーバーも収容する

`official-vps`には任意の`staging` instanceを用意している。`staging.enabled: true`にすると、本番とは別のdata、backup、OCI image、Paper、plugin lockを持つ`minecraft-dev` serviceを生成する。本番は`25565/tcp・udp`と`25575/tcp`、stagingは`25566/tcp・udp`と`25576/tcp`を使う。Scratch stableの既定接続先は`sb.mc-remote.com`、Scratch devは`sb-dev.mc-remote.com`となる。

`minecraft-dev`にはComposeの`staging` profileが付くため、通常の`docker compose up`では起動しない。必要なときだけ次のように起動する。

```sh
sudo docker compose --profile staging up -d minecraft-dev
```

停止中のinstanceだけを休眠として扱う。2つを同時に常時稼働する場合は、Minecraftのmain tick threadだけでなく、2つのheap、host memory、swap、disk I/Oを代表負荷で確認する。

## 暗号化したoff-host backup転送

最初のtransfer adapterは、ServerBackupのarchiveを公開age recipientで暗号化してから、明示的なFTPS sessionを開始する。証明書の検証を必須とし、data connectionを保護、passive modeを使用。一時的なファイル名でuploadした後にリモートでファイル名変更、最終的なファイルサイズを検証する。`--verify-download` を付けると、リモートの暗号文をダウンロード、そのSHA-256も比較する。平文と暗号化済みのローカルファイルはqueueに残り、転送処理後に削除しない。

```sh
uv run mcrctl backup transfer /backup/outbox/backup.zip \
  --project ./deployment \
  --verify-download
```

FTPS passwordは `secret://backup_ftps_password` として参照し、`mcrctl secret set` で保存。deployment projectには保存しない。VPSしか持たない利用者は、既存のSSH/SFTP経路を使ってoutboxのartifactをdownloadし、別の場所へuploadすることも可能。
このパッケージはVPSへFTP daemonをinstallしない。VPS内にだけ存在するsnapshotはlocal recovery stateであり、off-host backupではない。

秘密値を含む既存のサーバー全体のrecovery pointを、展開せずに調査するには次を実行する。

```sh
uv run mcrctl archive inspect /path/to/backup.zip --json
```

結果にはarchiveのSHA-256、ZIP CRCの検査結果、合計size、region数、rootにあるserver JARのidentity、使用中の`plugins/*.jar` のSHA-256が含まれる。Paperのremap cacheやplugin libraryも数えるが、使用中のpluginとして誤って報告しない。plugin設定の内容は表示しない。

deployment lockで指定したPaperとplugin JARだけをrecovery archiveから取り込むには、次を実行。

```sh
uv run mcrctl artifact import-archive /path/to/backup.zip --project ./deployment
```

このcommandはarchive全体のSHA-256を検証し、指定した各memberが一つだけ存在することを確認し、streamしながら各artifactのSHA-256を検証する。その後、対象のJARだけをcontent-addressed local storeへ保存する。world dataやplugin設定は展開しない。
`MC_REMOTE_ARTIFACT_HOME` でlocal storeの場所を変更でき、`--store` で明示的なSHA-256 store directoryを指定できる。

生成されたMinecraft Compose設定は、lockしたPaper JARを `PAPER_CUSTOM_JAR` でmountし、lockした各pluginをimageの`/plugins` attach pointへread-onlyでmountする。起動時には、`/data/plugins` 直下にある古いJARだけを削除してから、lockした一式を同期する。pluginのdata directoryは残しつつ、名前が変わって残った古いJARは除去される。Paper defaultのruntime downloadは無効で、生成済み設定を `/config` から同期する。

official profileは、`mc-remote.com` と `www.mc-remote.com` をCaddyのstatic siteとして生成。ホームページの内容は `/var/lib/mc-remote/homepage/sha256/<sha256>` からread-onlyでmountし、証明書、private key、ACME stateはCaddyの独立した `/data` に保存する。レンタルサーバーから回収したarchiveでは、`source_archive` provenanceを使い、取得元のSHA-256、
source root、意図的に除外したhost固有fileを記録できる。
