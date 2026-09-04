# mc-remote-stack

[English here.](README.md)

`mc-remote-stack` は、McRemote（マイクラリモコン）サーバーを再現可能な形で設置・運用するためのパッケージ。Scratchクライアントを含む。新設計の `mc-remote.toml`、または移行前の legacy `mc-remote.yml` から、検証済みでdigestを固定したruntime設定を生成する。

## 通常deployment：一つのfile、二つのcommand

通常経路では、検証済みのimmutable presetを選び、URL、接続先、任意のお知らせを一つの
`mc-remote.toml`へ書く。

```toml
schema_version = 1
deployment = "school-a"
preset = "classroom@1"

[surfaces]
scratch_url = "https://scratch.example.org/"
bridge_url = "wss://bridge.example.org/"

[[targets]]
id = "classroom"
label = "Classroom"
sandbox = "minecraft.example.org"
default = true
```

通常操作は次の二つである。`apply`がvalidate、preset解決、exact lock、artifact取得、render、
preflightを内部で行い、managed runtimeの有無からcreate／updateを自動判定する。`uv`の実体は
fresh host bootstrapが`$HOME/.local/bin/uv`へ配置し、以後のlogin sessionではcommand名だけで実行できる。

```sh
uv run mcrctl apply ./mc-remote.toml
uv run mcrctl doctor school-a
```

Scratch runtime schema、fixture、container mount path、Scratch image digestはpresetが固定するScratch contract
handoffから読み、operatorの別入力にはしない。`classroom@1`はScratch commit `4c893bd…`の不変contract treeと、
Scratch自身のCIがpublishしたScratch／Bridge image digestを固定する。Stackはtag／manifestをread-only照合して
lockするだけで、Scratch／Bridge／Pluginをbuildしない。前回Stackが起動したworkflowのimageは参照しない。
contract directory外のScratch source、product-config、探索版`home-server@7`／`compose@15`からfieldを取り込まない。

このプロジェクトは、次のものとは意図的に分離している。

- `mc-remote-knowledge`: 公開アーキテクチャと意思決定のSSOT（Single Source of Truth、信頼できる唯一の情報源）
- `mc-remote-backstage`: provider、契約、実ホスト、incident などの private ops。公開手順の依存先にはしない
- deployment project: instance固有のdesired state（望ましい状態）とlockデータ

## 正準 runbook

- [release artifact／preset準備](docs/release-preset-preparation-guide_ja.md): 指定releaseのcomponent handoffと
  公式配布元からexact artifact identityを照合し、append-onlyのimmutable presetへ固定する
- [agent-assisted bootstrap](docs/agent-assisted-bootstrap-guide_ja.md): agentを対象hostへ置かない
  基準経路、管理端末からのSSH支援、対象host上agentの限定実験とsecurity gate
- [fresh host bootstrap](docs/fresh-host-bootstrap-guide_ja.md): 個人管理者、SSH、exact Stack checkout、
  正準uv、Python、Docker、Composeを準備する一本道
- [public VPS deployment](docs/public-vps-bootstrap-guide_ja.md): review済みhandoffから
  plan、apply、doctorまでを上から実行するsame-volume release更新
- [通常dev環境](docs/normal-dev-environment-guide_ja.md): server側だけを別hostへ置き、開発者workstationの
  Minecraft／Scratch／Python／WireScopeから検証する`dev-integration`のpreflight、初回apply、更新経路
- [home private alpha検証](docs/home-alpha-validation-guide_ja.md)
- [Wake-on-LAN運用](docs/wake-on-lan-field-note_ja.md): 準24時間serverのpower state操作

## 設計資料

- [CLI検証環境の分担計画](docs/cli-validation-environment-plan_ja.md): ローカル、ケータリングPC、
  ホームサーバー、稼働中VPSの役割
- [ケータリング型検証roadmap](docs/catering-type-validation-roadmap_ja.md)
- [preset / lock 解決の詳細設計](docs/preset-resolution-design_ja.md): preset registry、preset catalog、compatibility evidence、lock identity。bundled home profile/preset、typed operator input、TOML init/resolve/fetch/renderのoperator経路を実装済み
- [TOML project layout の詳細設計](docs/toml-project-layout-design_ja.md): 一environment一project、includeなし、owner分離、lossless editing、YAML/TOML同居gate。明示的なvolume/world/network契約、`minecraft-motd@1`、managed renderを実装済み
- [deployment operator workflow redesign](docs/deployment-operator-workflow-design_ja.md): 運用者環境、root実行拒否、保存価値に沿ったin-place更新、release非依存のplan/apply、runbook更新規律
- [`home-beta` bootstrap apply設計](docs/home-beta-bootstrap-apply-design_ja.md): current lockとcanonical renderに固定したlocal Docker preflight、初回managed volume作成、Compose起動、container rollback。upgradeと既存world流用は未実装

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

## Public VPS deployment

公開VPSの正準手順は[public VPS release deployment runbook](docs/public-vps-bootstrap-guide_ja.md)である。
review済みhandoffがtarget写像、knowledge commit、Stack commit、deployment project、exact profile、
exact preset、authorized actionを一組で渡す。環境確認、plan、apply、doctorを上から順に実行する。

deployment projectの`mc-remote.toml`とexact lockが稼働中のdesired stateを識別する。次のexact setは、
handoffに記載されたcommitの`mc-remote-knowledge` release gate notesから受け取る。新しいUbuntu hostは先に
[fresh-host bootstrap](docs/fresh-host-bootstrap-guide_ja.md)を完了する。

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
