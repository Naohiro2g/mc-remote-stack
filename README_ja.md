# mc-remote-stack

[English here.](README.md)

`mc-remote-stack` は、McRemote（マイクラリモコン）サーバーを再現可能な形で設置・運用するためのパッケージ。Scratchクライアントを含む。新設計の `mc-remote.toml`、または移行前の legacy `mc-remote.yml` から、検証済みでdigestを固定したruntime設定を生成する。

このプロジェクトは、次のものとは意図的に分離している。

- `mc-remote-knowledge`: 公開アーキテクチャと意思決定のSSOT（Single Source of Truth、信頼できる唯一の情報源）
- `mc-remote-backstage`: provider、契約、実ホスト、incident などの private ops。公開手順の依存先にはしない
- deployment project: instance固有のdesired state（望ましい状態）とlockデータ

## 公開 runbook

- [agent-assisted bootstrap](docs/agent-assisted-bootstrap-guide_ja.md): agentを対象hostへ置かない
  基準経路、管理端末からのSSH支援、対象host上agentの限定実験とsecurity gate
- [fresh host bootstrap](docs/fresh-host-bootstrap-guide_ja.md): 個人管理者ユーザー、SSH、安全な開始点、現行 `mcrctl` の停止境界
- [Wake-on-LAN optional operation field note](docs/wake-on-lan-field-note_ja.md): 準24時間運用でWoLを
  重視しつつhardware要件にしない理由、directed broadcast、Python / `wakeonlan`、
  power stateごとの検証・証跡境界
- [旧 server-runbook の振り分け](docs/server-runbook-migration-notes_ja.md): carry した内容と、stale/history として採らなかった内容
- [preset / lock 解決の詳細設計](docs/preset-resolution-design_ja.md): preset registry、preset catalog、compatibility evidence、lock identity。bundled home profile/preset、typed operator input、TOML init/resolve/fetch/renderのoperator経路を実装済み
- [TOML project layout の詳細設計](docs/toml-project-layout-design_ja.md): 一environment一project、includeなし、owner分離、lossless editing、YAML/TOML同居gate。明示的なvolume/world/network契約、`minecraft-motd@1`、managed renderを実装済み
- [`home-beta` bootstrap apply設計](docs/home-beta-bootstrap-apply-design_ja.md): current lockとcanonical renderに固定したlocal Docker preflight、初回managed volume作成、Compose起動、container rollback。upgradeと既存world流用は未実装

旧 `server-runbook` の native-systemd / package Caddy / release-symlink 手順は、現在の
Compose・生成設定中心の実装と一致しないため現行 runbook として取り込みません。

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

## `home-beta` TOML operator経路

最初の新設計projectは、一environmentだけを明示して初期化する。directory名からidentityやchannelを
推測せず、EULA同意、resolve、artifact取得をそれぞれ独立した操作にする。instance固有order / lockは
package source checkoutの外にある独立projectへ置く。TOML `init`はproject rootを最大`0750`、
初期fileを最大`0640`で作り、呼出し元のより厳しいumaskは維持する。

```sh
MC_REMOTE_PROJECT="$HOME/mc-remote-deployments/home-beta"
uv run mcrctl init "$MC_REMOTE_PROJECT" \
  --format toml \
  --deployment-name home \
  --profile home-server@2 \
  --environment-identity home-beta \
  --channel beta \
  --exposure isolated \
  --purpose integration \
  --preset mcremote-paper@1 \
  --artifact-store "$HOME/.local/share/mc-remote/artifacts" \
  --volume minecraft-data=home-beta-minecraft-data \
  --world-identity home-beta-world \
  --bind-address 127.0.0.1 \
  --java-port 25565 \
  --mcremote-port 25575
```

server listの公開表示文を変更する場合だけ、`mc-remote.toml`へ次を追加する。

```toml
[[operator_inputs]]
role = "minecraft-motd"
adapter = "minecraft-motd@1"
path = "operator/minecraft-motd/server.properties"
```

同時に`operator/minecraft-motd/server.properties`を作る。これは公開情報専用のstrictな
typed inputであり、secretを書かない。commentと空白だけの変更はlock identityを変えない。

```properties
# Public server-list text
motd=McRemote home beta
```

operator inputを追加した場合も、resolveより先にvalidateする。

```sh
uv run mcrctl validate --project "$MC_REMOTE_PROJECT"
uv run mcrctl accept-eula --project "$MC_REMOTE_PROJECT" --yes
```

bundled `mcremote-paper@1` は、compatibility evidenceがまだ揃っていないため `unverified` である。
bootstrapする場合だけ、`mc-remote.toml` の
`acknowledgements.allow_unverified = true` と具体的な `unverified_reason` を人間が記録し、
one-shot flagを付けて解決する。

```sh
uv run mcrctl resolve --project "$MC_REMOTE_PROJECT" --allow-unverified
uv run mcrctl plan --project "$MC_REMOTE_PROJECT"
uv run mcrctl artifact fetch --project "$MC_REMOTE_PROJECT"
uv run mcrctl render \
  --project "$MC_REMOTE_PROJECT" \
  --output "$MC_REMOTE_PROJECT/generated"
```

`artifact fetch` はcurrent lockに列挙されたHTTPS fileだけを取得し、
`<artifact_store>/sha256/<digest>` へ検証済みbytesを保存する。既存entryも毎回再hashする。
OCI imageをpullせず、`render` もCompose起動・volume作成・server接続を行わない。
`plan` はunverified警告がある間は内容を表示してstatus 1を返す。

初回のisolated `home-beta`だけは、対象host上のlocal Unix Docker contextへbootstrap applyできる。
`PLAN lock=unchanged identity=...`でreviewした値を手入力し、ambient contextや別lockを使わない。

```sh
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

applyはexact OCI imageをpullし、未知container・未知volume・port衝突を拒否した後にだけmanaged
volumeとMinecraft serviceを起動する。失敗時はcontainerをdownするが、world volumeは削除しない。
Docker導入、firewall変更、既存world import、upgradeはこのcommandの対象外である。

ログイン後のread-only稼働確認には`apply`を再利用せず、`doctor`を使う。

```sh
uv run mcrctl doctor --project "$MC_REMOTE_PROJECT"
```

既定では`<project>/generated`とlocal Docker context `default`を使う。doctorはcurrent lockとcanonical
render、managed volume、container label、running / healthy、lockどおりのloopback port、
token無しprotocol helloを確認する。container logやsession / player / tokenを通常出力へ載せない。
compatibilityがまだ`unverified`なら、runtimeがhealthyでも警告は残る。

`home-alpha` は後から別projectとしてinitし、別volume identity・別world identityを与える。
`home-beta` のdirectoryやlockをcopyして追加しない。

## Legacy `official-vps` 垂直スライス（回帰用）

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
このlegacy経路は当面、新TOML設計とのplan/render比較に使う回帰fixtureである。最初のhome live
deploymentには使わず、bootstrap applyも受理しない。

初期化したlockは、意図的に特定versionへ固定していない。profileが選ぶものはトポロジーとポリシーであり、マイクラやマイクラリモコンのバージョンではない。このため既存サーバーを移行するときは、回収した現物ファイル（バージョン）を固定するため、インフラ移行と同時にMcRemoteのupgradeを強制されずに済む。

### 同じVPSへ開発サーバーも収容する

`official-vps`には任意の`staging` instanceを用意している。`staging.enabled: true`にすると、本番とは別のdata、backup、OCI image、Paper、plugin lockを持つ`minecraft-dev` serviceを生成する。本番は`25565/tcp・udp`と`25575/tcp`、stagingは`25566/tcp・udp`と`25576/tcp`を使う。Scratch stableの既定接続先は`sb.mc-remote.com`、Scratch devは`sb-dev.mc-remote.com`となる。

`minecraft-dev`にはComposeの`staging` profileが付くため、通常の`docker compose up`では起動しない。6GB VPSではprod/devを同時起動せず、生成された排他切替scriptを使う。scriptは1分前から告知し、`save-all flush`、graceful stop、接続確認を行い、失敗時は元のinstanceへ戻す。

```sh
sudo bash /etc/mc-remote/generated/operations/use-staging.sh
sudo bash /etc/mc-remote/generated/operations/use-production.sh
```

停止中のinstanceだけを休眠として扱う。2つを同時に常時稼働する場合は、排他切替を外す前にMinecraftのmain tick threadだけでなく、2つのheap、host memory、swap、disk I/Oを代表負荷で確認する。

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
