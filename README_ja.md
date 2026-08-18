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
- [CLI検証環境の分担計画](docs/cli-validation-environment-plan_ja.md): ローカル、ケータリングPC、
  ホームサーバー、稼働中VPSの役割と安全な検証順
- [fresh host bootstrap](docs/fresh-host-bootstrap-guide_ja.md): 個人管理者ユーザー、SSH、安全な開始点、現行 `mcrctl` の停止境界
- [public VPS bootstrap](docs/public-vps-bootstrap-guide_ja.md): `vps-server@5`のread-only discovery、
  exact multi-service plan/apply、public doctor、既存host cutover、残るreadiness phase
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
  --profile home-server@4 \
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

exact `home-server@4` + `mcremote-paper@1` subjectは、認証強制込みのcompatibility evidenceが
まだ揃っていないため`unverified`である。
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

既定では`<project>/generated`とlocal Docker context `default`を使う。doctorはcurrent lockと
generated bytes、running containerのCompose provenance、managed volume、container label、
running / healthy、lockどおりのloopback port、token無しprotocol helloが`auth_required`で
拒否されることを確認する。token無しhelloが成功した場合は`doctor_auth_not_enforced`でFAILする。
canonicalなgenerated `compose.yaml`以外も実行時に使われていれば、runtime / protocol検査は
継続するが`WARN render=additional-compose-files`と報告する。container logやsession /
player / tokenを通常出力へ載せない。compatibilityがまだ`unverified`なら、runtimeがhealthyでも
警告は残る。

`home-alpha` は後から別projectとしてinitし、別volume identity・別world identityを与える。
`home-beta` のdirectoryやlockをcopyして追加しない。

## Public VPS beta（新TOML vertical slice）

`vps-server@5`は、exact `public-web-paper@1`のCaddy、Scratch、Bridge、Minecraft、
Paper、McRemoteを構築する現行ケータリング型VPS profileである。Caddyだけをpublic edgeへ
接続し、backendはinternal app networkへ限定する。

host firewall、provider firewall、DNSはproject外の人間checkpointであり、`apply`は変更しない。
EULA、unverified理由、exact lock、canonical renderをreviewした後、対象VPS上のlocal Docker
contextで`--bootstrap --yes`を明示してapplyする。失敗時はcontainerをdownするがmanaged world
volumeは保持する。`doctor`はpublic bind、current lock / render、managed multi-service
runtime、protocol helloをread-onlyで検証する。外部HTTPS / WSS readinessと
content-addressed homepageは後続claimである。

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

現行の公開beta TOML経路は`vps-server@7`を使い、Scratch runtimeの非空`connection_targets`を
必須にする。betaのdefaultは`sb-beta.mc-remote.com`で、公開ベータnoticeもruntime configへ投影する。
欠落やdefault不包含はresolve / render / doctorでfail closedになる。

`minecraft-dev`にはComposeの`staging` profileが付くため、通常の`docker compose up`では起動しない。6GB VPSではprod/devを同時起動せず、生成された排他切替scriptを使う。scriptは1分前から告知し、`save-all flush`、graceful stop、接続確認を行い、失敗時は元のinstanceへ戻す。

```sh
sudo bash /etc/mc-remote/generated/operations/use-staging.sh
sudo bash /etc/mc-remote/generated/operations/use-production.sh
```

停止中のinstanceだけを休眠として扱う。2つを同時に常時稼働する場合は、排他切替を外す前にMinecraftのmain tick threadだけでなく、2つのheap、host memory、swap、disk I/Oを代表負荷で確認する。

## 暗号化したoff-host backup転送

transfer adapterは、ServerBackupのarchiveを公開age recipientで暗号化してから、明示的なFTPS sessionを開始する。証明書の検証を必須とし、data connectionを保護、passive modeを使用。一時的なファイル名でuploadした後にリモートでファイル名変更、最終的なファイルサイズを検証する。`--verify-download` を付けると、リモートの暗号文をダウンロード、そのSHA-256も比較する。復元が転送元VPSに依存しないよう、秘密値を含まないtransfer record sidecarも暗号文と一緒に保存する。平文と暗号化済みのローカルファイルはqueueに残り、転送処理後に削除しない。

```sh
uv run mcrctl backup transfer /backup/outbox/backup.zip \
  --project ./deployment \
  --transport-config /secure/path/backup-transport.toml \
  --verify-download
```

定期実行では、既存世代を暗黙に搬送対象へ含めないよう、運用開始時にactivation markerを
作る。`drain`はmarkerより新しく、mtimeから120秒以上経過し、ZIP CRC検査中にidentity・
size・mtimeが変化しなかったarchiveだけを順番に搬送する。各搬送はremoteから暗号文を
再downloadしてSHA-256を検証する。`download-verified`のlocal transfer recordがある
archiveは再搬送しない。

```sh
install -m 600 /dev/null /secure/state/backup-transfer-activated

uv run mcrctl backup drain /backup/outbox \
  --after /secure/state/backup-transfer-activated \
  --project ./deployment \
  --transport-config /secure/path/backup-transport.toml
```

marker作成とtimer等への永続登録はoperator checkpointである。markerは既存archiveを
調べた後、初回自動実行より前に一度だけ作る。`drain`は平文、local暗号文、transfer
record、remote世代を削除しない。local queueのretentionはsnapshotの世代保持と分けて
明示的に決める。

TOML deploymentではprovider / account inventoryをmode `0600`のprivate transport fileへ
分離する。legacy YAML deploymentは移行中の埋め込みtransport tableを引き続き利用できる。
どちらにもpassword値は保存しない。

復元する世代は必ず明示的に選択する。完了済み暗号文を一覧し、選んだrecordと暗号文を取得した後、
復号して元の平文SHA-256を検証する。

```sh
uv run mcrctl backup list --project ./deployment \
  --transport-config /secure/path/backup-transport.toml

REMOTE_NAME='backup.zip.<encrypted-sha256>.age'
uv run mcrctl backup download-record "$REMOTE_NAME" \
  --project ./deployment \
  --transport-config /secure/path/backup-transport.toml \
  --output ./recovery/backup.transfer.json
uv run mcrctl backup download "$REMOTE_NAME" \
  --project ./deployment \
  --transport-config /secure/path/backup-transport.toml \
  --record ./recovery/backup.transfer.json \
  --output ./recovery/backup.zip.age
uv run mcrctl backup decrypt ./recovery/backup.zip.age \
  --record ./recovery/backup.transfer.json \
  --identity /secure/path/age-identity.txt \
  --output ./recovery/backup.zip
uv run mcrctl archive inspect ./recovery/backup.zip --json
```

`backup list`の`record=present`は、暗号文とremote recovery sidecarが組で存在することを示す。
`record=missing`は旧形式または不完全な搬送であり、remoteだけから`download-record`を開始できない。
一覧から存在を隠さないが、復元可能とは主張しない。

これらのcommandは`latest`を暗黙に選択せず、remote世代を削除せず、既存のlocal出力を
上書きせず、FTPS passwordやage identityを表示しない。age identityはdeployment projectと
Gitの外に保管する。

FTPS passwordは `secret://backup_ftps_password` として参照し、`mcrctl secret set` で保存。deployment projectには保存しない。VPSしか持たない利用者は、既存のSSH/SFTP経路を使ってoutboxのartifactをdownloadし、別の場所へuploadすることも可能。
このパッケージはVPSへFTP daemonをinstallしない。VPS内にだけ存在するsnapshotはlocal recovery stateであり、off-host backupではない。

秘密値を含む既存のサーバー全体のrecovery pointを、展開せずに調査するには次を実行する。

```sh
uv run mcrctl archive inspect /path/to/backup.zip --json
```

結果にはarchiveのSHA-256、ZIP CRCの検査結果、合計size、region数、rootにあるserver JARのidentity、使用中の`plugins/*.jar` のSHA-256が含まれる。Paperのremap cacheやplugin libraryも数えるが、使用中のpluginとして誤って報告しない。plugin設定の内容は表示しない。
pluginがPaper runtime libraryとして宣言したcoordinateは`runtime_libraries`として報告する。
これはdownload宣言のinventoryであり、transitive contentがlock済みであるとは主張しない。

選択したworld rootだけをcurrent TOML deploymentへ復元するには、次を実行する。

```sh
uv run mcrctl world restore plan ./recovery/backup.zip \
  --project ./deployment \
  --output ./deployment/generated \
  --source-world world \
  --expected-archive-sha256 '<64-lowercase-hex>' \
  --expected-lock-identity 'sha256:<64-hex>'

uv run mcrctl world restore apply ./recovery/backup.zip \
  --project ./deployment \
  --output ./deployment/generated \
  --source-world world \
  --expected-archive-sha256 '<64-lowercase-hex>' \
  --expected-lock-identity 'sha256:<64-hex>' \
  --yes
```

このtransactionは、危険または重複したZIP entryとsymlinkを拒否し、overworldと存在する
Nether / End rootだけをstagingする。cutover時にはMinecraftだけを停止し、current lockの
serviceを起動してdoctorを実行する。plugin dataとcredentialは展開しない。起動またはdoctorが
失敗した場合は旧world rootを戻す。成功時も、operator検証が終わるまで旧rootを報告された
rollback directoryへ保持する。追加Compose fileで起動したcontainerもapply前に拒否する。
canonical renderだけで再起動して、overrideが供給するservice、mount、pluginを暗黙に外すことを
防ぐためである。

これは本リポジトリが管理するrecovery commandのwrite set契約である。`world restore`がmanaged
Minecraft data volumeへ書き込むのは選択したworld rootだけであり、`artifact import-archive`が
書き込むのはcontent-addressed artifact store内のlock指定JAR bytesだけである。どちらも
`plugins/McRemote/`へ書き込まない。手動または一時的なplugin data recoveryは別操作であり、
world restore契約には含めない。

現行の`@server` whole-server archiveは`/data`内のplugin dataを含むsecurity-sensitiveなruntime
stateである。transfer adapterがoff-hostへ送るのはage暗号文だけとし、平文のretention、recipient
access、age identityをoperatorが明示管理する。archiveに含まれることは、plugin dataがworld restore
契約へ入ることを意味しない。

credentialをworldから分離する`home-server@3` / `compose@5`は、credential snapshotとrevocation
authorityをそれぞれ`/data`外の独立volumeへmountする。これによりworld restoreと`/data`だけのarchive
から両方を除外する。exact b3 presetと、session token永続化を含むisolated alpha用
`mcremote-paper@6`は実装済みである。`@6`のMcRemote JAR SHA-256は
`331633ef15a729658496e89fe49cb8a5eb5ebcb2ec86937b7e5313528d7ec997`で、controlled bootstrapは
`alpha` / `isolated` / `integration`の組合せだけを許す。home-alphaではfresh credential bootstrap、
同一b4再起動後のsession再利用、空world・新規pairingでのScratch／Python建築コード再実行を確認した。

pluginのnonce付き機械可読checkpointとそのdoctor consumer、一般向けbootstrap／reset transaction、
long-lived credential公開gateは後続sliceである。現時点のdoctorはmount topology検査後に
`doctor_credential_health_unsupported`でfail closedする。これはunsupportedを健康と誤認しない境界であり、
一般profileの公開既定化を承認するものではない。一方、knowledgeのauthentication roadmapどおり、
この後続credential-lifecycle sliceでb4利用者機能を律速しない。

起動logに明示されたruntime dependency downloadとupdate checkを、raw lineやURL pathを
再出力せず分類するには、次を実行する。

```sh
uv run mcrctl runtime audit-log ./minecraft-startup.log --json
```

Paper library download、Geyser型runtime content download、update checkを識別する。
一致eventが無いことは、pluginがnetwork requestを行っていない証明にはならない。

deployment lockで指定したPaperとplugin JARだけをrecovery archiveから取り込むには、次を実行。

```sh
uv run mcrctl artifact import-archive /path/to/backup.zip --project ./deployment
```

このcommandはarchive全体のSHA-256を検証し、指定した各memberが一つだけ存在することを確認し、streamしながら各artifactのSHA-256を検証する。その後、対象のJARだけをcontent-addressed local storeへ保存する。world dataやplugin設定は展開しない。
`MC_REMOTE_ARTIFACT_HOME` でlocal storeの場所を変更でき、`--store` で明示的なSHA-256 store directoryを指定できる。

生成されたMinecraft Compose設定は、lockしたPaper JARを `PAPER_CUSTOM_JAR` でmountし、lockした各pluginをimageの`/plugins` attach pointへread-onlyでmountする。起動時には、`/data/plugins` 直下にある古いJARだけを削除してから、lockした一式を同期する。pluginのdata directoryは残しつつ、名前が変わって残った古いJARは除去される。Paper defaultのruntime downloadは無効で、生成済み設定を `/config` から同期する。

official profileは、`mc-remote.com` と `www.mc-remote.com` をCaddyのstatic siteとして生成。ホームページの内容は `/var/lib/mc-remote/homepage/sha256/<sha256>` からread-onlyでmountし、証明書、private key、ACME stateはCaddyの独立した `/data` に保存する。レンタルサーバーから回収したarchiveでは、`source_archive` provenanceを使い、取得元のSHA-256、
source root、意図的に除外したhost固有fileを記録できる。
