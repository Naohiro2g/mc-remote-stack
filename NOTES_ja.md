# mc-remote-stack NOTES

確定前または別sliceへ送る作業だけを置く。private host名、IP、credential、account情報は書かない。

## 2026-08-07 b3 release gate — McRemote協調checkpoint

- [x] knowledge remote `main` commit `3dfbf57c07f2b7985c65edc5564b879f9e67e122`の最新dev agent
  runtime protocol、`00-hub/release-gate-notes_ja.md`、`10-protocol/versioning-design_ja.md`
  §10.11.1項14・15／§10.11.2、`11-plugin/platform-design_ja.md` §9を照合した。b3 scopeはcatalog一式と
  Scratch `.sb3` save/load互換のままで、credential lifecycle／checkpoint、DoS guard、PoPを混ぜない。
- [x] McRemote source `a3dab998b710f65f42f95058a68ec51d419b097c`からtag
  `v1.21.11-2100.0.0b3`とGitHub prereleaseが作成された。asset
  `mc-remote-1.21.11-2100.0.0b3.jar`はsize `138178`、SHA-256
  `aeb190705bd9957ce73557dc1be0fe15efe7250ba9bc688945e6f537e00ef78e`。Stack自身のfetcherで再取得し、
  release APIのdigest、作業依頼票、content-addressed store上の再計算値が一致した。
- [x] append-onlyな`mcremote-paper@3`を追加し、`home-server@3`の
  `credential-rollback-separated` capabilityだけで解決できるisolated alpha候補として固定した。
  `home-server@4`等、二backend roleを持たないprofileとのresolveは`profile_incompatible`で拒否する。
  compatibilityは`unverified`、required claimsは`profile-render` / `protocol-hello`のままである。
- [x] 一時projectでresolver、plan、artifact fetch、compose@5 canonical renderを実行した。lock
  `sha256:20532d350082fed32edec01c2757951f6b335d750b64366d50f1c526d5c544fe`、preset content
  `f45ef27adb1dd80d1dda5cd80ddf16ee35332d72315751b48f0fbde8fbe38d19`。renderはauth enforced、
  `/mcremote/credential-store`と`/mcremote/credential-revocations`の別volume、exact b3 JAR bindを持つ。
- [ ] alpha applyはまだ行わない。b3 pluginは両backendが空のfresh起動を`UNINITIALIZED`として明示
  credential bootstrapを要求する一方、Stackのbootstrap / reset transactionとcredential health
  checkpoint consumerは未実装である。review済みlock、resource作成、plugin所有bootstrap surface呼出し、
  失敗時の再試行境界を人間checkpoint込みで固定してからapplyする。
- [ ] apply後はsanitized live-humanでLuckPerms effective meta＝hello `permissions.buildRange`＝実build
  guardを確認する。long-lived公開gateを閉じるには別途live rollback / revoke evidenceが必要で、b3 catalog
  gateと同一視しない。正式record／artifactのauthoringはknowledge側へ搬送し、private host名、player名、
  UUID、token、pair codeはStackへ残さない。

## 2026-08-06 session close — b3 release gate再開点

- [x] official public beta環境の修復は完了した（運用者確認）。これはb2環境の復旧完了であり、
  b3 release gateの合格やb3 artifactの確定を意味しない。
- [x] knowledgeの最新dev agent runtime protocolを取得後、同commitの
  `00-hub/release-gate-notes_ja.md`、`10-protocol/versioning-design_ja.md` §10.11.2、b3定義を読み、
  b3 release gate確認から再開した。
- [x] McRemote effective build range修正の21件PASSとbuild成功は、knowledge main
  `97921f0626b00e0719801b7695769df1fea243e3`の`00-hub/NOTES_ja.md`にローカル進捗として
  捕捉した当時の混在JARは正式証跡に使わず、後続のsource commit `a3dab998b710f65f42f95058a68ec51d419b097c`
  とGitHub prerelease assetへ置き換えた。
- [ ] McRemoteのimmutable artifact作成とStack `mcremote-paper@3` exact-pinまでは完了した。残る順序は
  明示credential bootstrap / health境界を固定したalpha apply、sanitized live-human再検証である。
- [x] credential checkpoint契約はknowledge `2026-08-06-02`へ着地確認OK。これはlong-lived
  credential gateの別sliceであり、契約着地や未実装checkpointをb3 scope／release gateへ混ぜない。
  checkpoint実装の未完了は直下の決定参照節で別途追跡する。

## 2026-08-06 credential health checkpoint [→DEC 2026-08-06-02]

- [x] 正式参照`Naohiro2g/mc-remote-knowledge` / commit
  `97921f0626b00e0719801b7695769df1fea243e3` / `00-hub/DECISIONS_ja.md` /
  `2026-08-06-02`と、`11-plugin/platform-design_ja.md` §9.9への着地を搬送票と照合した。
  決定、理由、却下案、McRemote／Stackの担当分担は一致しており、着地確認OK。
- [x] projectionはconsole-onlyの明示checkpoint応答としてだけ生成する。heartbeat、定期／自発更新、
  RCON、wire、Stackによる内部JSON解析・生成・修復は使わない。McRemoteはbackendを変更せず完全再検証し、
  `/data/plugins/McRemote/credential-health.json`へ同一doctor runの非null nonceをatomic publishする。
- [ ] McRemote／Stackは同じschema-v1 fixtureでnested object shapeとenum語彙をtest-first固定する。
  fixtureと双方のwriter／parser testが揃うまで、Stack doctorはcheckpointを利用可能と主張しない。
- [ ] Stackはdeployment単位のdoctor直列化、nonce、bounded read／retry、16 KiB・UTF-8・regular-file／
  non-symlink・schema／field整合のfail-closed検証を実装する。`CREATE_CONSOLE_IN_PIPE=true`とruntime
  UID／GIDをpreset／lock／renderとcontainer実体で再現可能に照合する。
- [ ] bootstrap／reset transactionは別設計の未確定事項として残し、外部transaction IDをcheckpointへ
  混ぜない。契約着地だけではlong-lived credential gateを開かず、両repo実装、immutable artifact／
  preset、sanitized live、live restore後のauthority継続まで正式証跡を揃える。

## 2026-08-06 McRemote effective build range解決の修正設計 [→DEC 2026-08-06-01]

- [x] official public betaのlive-human確認で、LuckPerms user effective metaにユーザー直設定500と
  primary group由来100があるとき、McRemote b2 helloが`permissions.buildRange: 100`を返すことを
  観測した。token、pair code、UUID、player名、private host情報は公開記録へ残さない。
- [x] b2 release commit `4b75007330c58eddd4b06d67415d05958f661c7c`の
  `LuckPermsPermissionManager#getPlayerRange`が`User`をloadした後、primary groupの
  `Group#getCachedData()`だけを読むため、user direct metaを無視すると特定した。
- [x] 正式参照
  `Naohiro2g/mc-remote-knowledge` / commit `1bf4d09ee755760d028675eb4a4baa1ec4a0a0cd` /
  `00-hub/DECISIONS_ja.md` / `2026-08-06-01`を会話内で受領した確定搬送票と照合した。McRemoteは
  既存QueryOptionsのまま`User`のeffective meta解決へ委譲し、primary group、user、
  継承group、context、weightの順序を独自実装しない。wire field、protocol 21.0.0、auth、pairing、
  欠落・parse失敗時0、LuckPerms不在時fallbackを変更せず、負値、context再設計、fallback既定値を
  本修正へ混ぜない。決定、維持事項、却下案、McRemote／Stackの担当分担は一致しており、
  knowledge remote `main`への着地確認OK。
- [x] McRemote source `a3dab998b710f65f42f95058a68ec51d419b097c`のtest-first修正を含む
  GitHub prerelease artifactをStack `mcremote-paper@3`へexact-pinした。sanitized live-humanの
  effective meta＝hello buildRange＝実build guard再検証は未完了である。

## 2026-08-05 b2 auth enforcement deployment correction

- [x] McRemote b2 artifactの同梱`config.yml`が開発用`auth.enforcement=false`のまま公開されている一方、
  knowledge main `2fe1b503c86912ef8416d2e659e22381b273f194`のversioning §10.11.1項5は
  「トグルONがb2完了ゲート、リリース既定はenforced」と定めていることを照合した。
- [x] 公開VPSの旧`vps-server@4` / `compose@4`とhome private alphaの旧`home-server@2` /
  `compose@1`はいずれもMcRemote configを生成せず、実機doctorは双方とも
  `auth=not-required`だった。home alphaはruntime自体はhealthy / currentだが、trusted checkoutが
  alpha preset追加前のcommitへ戻っており、通常doctorは`unknown_preset_revision`で停止した。
- [x] append-onlyな修正として`home-server@4` / `compose@6`と`vps-server@5` / `compose@7`を追加し、
  `mcremote-auth-enforced`をprofile capability / required security controlへ固定した。両rendererは
  b2向け`plugins/McRemote/config.yml`を生成し、`auth.enforcement=true`を設定する。
- [x] 旧alpha / 旧public bootstrap contractを閉じ、新profileだけを新規apply対象にする。doctorは
  新security controlに加え、既存lock内のexact b2 artifact SHAも認識し、token無しhello成功を
  `doctor_auth_not_enforced`でfail closedにする。
- [x] `mcrctl migration auth-enforcement plan/apply`を実装した。planは旧lock / runtime /
  managed volumeと新`home-server@4` / `vps-server@5`候補をread-only検査する。VPSの追加Composeは
  exact path / SHA-256 / composition identityへ固定した場合だけ保存できる。applyは新volumeへcopyし、
  phaseをatomic JSONへ記録する。失敗時に旧runtimeへ自動復帰せず、同じexact transactionの再applyで
  target成功へ収束させる。旧volumeは削除しない。
- [x] official public betaを旧lock
  `sha256:979a677b3fb9c83a1ed5e1704c60f3054bcc8ae3e758a8877df9134b502f4d56`から`vps-server@5`の
  target lock`sha256:521d12ba568ac404c3fb91464a258d215263695d63f994aa93e71971f9f53377`へ
  live migrationした。review済みrecovery Composeを保存し、新しい
  3 volumeへcopyした後、target volume内のMcRemote configをenforced設定へ固定して再起動した。
  transactionは`phase=complete`となり、doctorでtoken無しhelloの`auth_required`までPASSした。
  compatibilityはone-shot acknowledgement付き`unverified`のままで、旧volumeは保持している。
- [ ] 既存home alphaで隔離live migrationを実証する。official public betaの追加plugin / homepageを
  preset / lock / renderへ正規化する作業も別途必要である。今回の限定的なdeployed-state保存を
  canonical migration完了と同一視しない。

## 2026-08-02 long-lived credential rollback resistance [→DEC 2026-08-02-01]

- [x] knowledge commit `9dd99f3aecccf4035cdfd5549d1e70f9f25d3b2d`の`2026-08-02-01`、
  `11-plugin/platform-design_ja.md` §9、`10-protocol/versioning-design_ja.md` §10.11.2への着地を
  元搬送票・stack実装と照合した。stack側の設計投影は
  [`docs/credential-rollback-resistance-design_ja.md`](docs/credential-rollback-resistance-design_ja.md)。
- [x] 個別revokeと衝突するglobal epochではなく、snapshotと別rollback domainのcreate-only
  revocation authorityを採る。snapshot rollback時もauthorityのtoken hash / credential ID
  tombstoneを認証・list・limitへoverlayする。authority不明のhost全損は新domain＋全失効・再pair。
- [x] revokeの線形化点はtombstone file `fsync`、finalへの非上書きpublish、authority directory
  `fsync`の完了とする。それ以後のsnapshot projection更新失敗はrevoke成功を覆さず、health degraded
  ＋reconcileとする。revoke commandで`credential_store_unavailable`を返すのはauthority durable
  commit前の失敗に限る（起動・read時にstateを検証できない場合にも同reasonを使用）。
- [x] 空snapshotでもdomainを検証できるsnapshot headerとauthority manifestを持つ。file backendは
  server生成UUIDだけをfinal名に使い、同一directoryの`CREATE_NEW` temporary、非上書きpublish、
  symlink拒否、既存finalの同一性検証、破損・ID/hash矛盾のfail-closeを契約にする。
- [x] plugin側は`CredentialStore`に加えて`RevocationAuthority`境界、domain mismatch fail-close、
  durable tombstone、既存session終了を実装した。b3 release sourceは
  `a3dab998b710f65f42f95058a68ec51d419b097c`、GitHub prerelease assetのSHA-256は
  `aeb190705bd9957ce73557dc1be0fe15efe7250ba9bc688945e6f537e00ef78e`である。
- [x] stack側は`home-server@3` / `compose@5`で二backend roleと保存resourceを表現する。既定構成は
  `credential-store`と`credential-revocations`を`/data`外の別volumeへ置くことを推奨するが、物理
  二volumeをplugin起動条件にしない。alpha統合試験でrevoke拒否を強制するためprofile 3だけ
  `auth.enforcement=true`とする。Authorityのvolume kindは`security-state`として通常runtime dataから
  区別し、現行`home-server@2` / `compose@1`へ破壊的追加しない。
- [ ] stackはprofileに従うresource作成・mountと明示bootstrap / resetの承認・transactionだけを担い、
  domainやplugin内部JSONを生成しない。pluginが形式の正本となる。二backend初期化の途中状態は通常
  起動で自動修復せず、双方空または同一bootstrap transactionと検証できる場合だけ再試行する。
- [ ] McRemote実装とstack profileを接続する前に、機械可読で非秘密なcredential health projectionと
  互換versionを両repoで固定する。現commitの`/mcremote credential status`とstartup logは人間向けで
  Stack doctor / bootstrap transactionの安定契約にはできない。projectionはplugin自身が検証した
  `UNINITIALIZED` / `HEALTHY` / `DEGRADED` / `UNHEALTHY`、snapshot / authority存在、domain一致を返し、
  domain ID、credential ID、token hash、UUIDは返さない。projection取得不能はStack側のunavailable
  errorとし、stackはplugin内部JSONを読み書きしない。
- [x] doctorはlockどおりの三volume identity、writable mount、`/data`外のexact pathをlive containerで
  検査する。domain / store health判定は前項のprojection待ちであり、mount healthyだけでcredential
  healthyとは報告しない。
- [x] bundled `mcremote-paper@3`へexact b3 release assetを固定し、`home-server@3`だけで
  resolver / fetch / renderできるunverified alpha contractを追加した。実apply contractは明示bootstrap /
  reset transactionとcredential health consumerが未実装のため、一般運用にはまだ開かない。fresh stateの
  controlled live testに限り、理由付きunverified acknowledgement、review済みlock、plugin consoleによる
  一度だけのbootstrapを要求してinitial applyを許可する。手順は
  [`docs/b3-credential-isolated-alpha-validation-guide_ja.md`](docs/b3-credential-isolated-alpha-validation-guide_ja.md)。
- [ ] exact plugin artifactとstack profileを固定したcross-repo `live-auto`で、準備済みA / BのA revoke→
  snapshotだけrollback→再起動を行い、A拒否・B成功・list / limit / current除外、authority継続、
  backup非包含、doctor healthyを一つのtransactionとして検証する。plugin単体のcrash / I/O fault試験を
  このlive testの代用にせず、逆にstackからfile backend内部faultを注入しない。
- [ ] 2026-08-07のb3 isolated alpha初回bootstrapで、fresh credential volume rootが`0:0 / 0755`の
  ままmountされ、UID 1000のMcRemoteがbootstrap markerを作れず`UNHEALTHY`へfail closedすることを
  live確認した。credential record / tombstoneは作成されず、既存二環境はhealthyを維持した。回帰修正は
  compose@5のruntime UID/GIDを`1000:1000`へ固定し、新規credential volumeだけをexact runtime imageの
  networkなし・read-only・CHOWN-only helperで起動前初期化する。現検証volumeは手動修復せず、修正merge後に
  空のisolated projectとして再作成する。
- [ ] 公開gateを閉じる際は、`/mcremote pair`を含む実際の`mcrl_`発行とdevice別revoke / logoutを
  `live-human`で一度確認し、token・pair code・UUIDをredactした正式evidenceをknowledgeへ搬送する。
- [ ] plugin実装→stack profile / renderer→`2026-08-02-03`のlive rollback gateの順で閉じる。
  authority自体をrollbackする脅威には外部単調状態が必要であり、offline
  cateringのhost全損ではcredential継続を主張しない。VPSで継続性を主張する場合はrevoke成功前の
  off-host同期durable commitを要求し、remote freshnessを証明できない復旧は全失効へ倒す。
- [x] stack / McRemote双方の着地確認OK後、2026-08-03に一時handoff materialをcleanupした。
- [ ] 2026-08-04 session close時点で、knowledge main
  `16e888376114b73609c75b02ca028fc414545a04`の契約とは一致するが、
  `11-plugin/platform-design_ja.md` §9.9と`2026-08-02-03`の実装状況は、まだplugin本体・二backend
  profile未実装という古い記載である。両source repoのcommit / pushとcross-repo検証後に進捗を更新する。
  `2026-08-02-08`の`mcrs_`再起動継続は別実装sliceとして残し、long-lived完了へ混ぜない。

## 2026-07-31 Bridge共有単位／connection_targets schema、knowledge着地確認OK [→DEC 2026-07-30-03]

- Bridge共有はクラスタ単位（同一セッションでの行き来が要る場合だけ共有）、`connection_targets`は
  `{id, label, sandbox}`で単一`bridge_url`共有、起動時URLは`id`選択限定——という設計を
  `2026-07-30-03`としてknowledge側へ着地確認済み（commit `065ff0dc7ef2f0c45035667a6446c5cfa6609a7f`）。
  DECISIONS新規行・wire-format-design§2追記・scratch-roadmap§2.1/§2.2追記を搬送票と照合し、
  決定内容・却下案・影響範囲とも一致を確認した。
- mc-remote-stack側の実装は完了・commit・push済み（`36b1113`）。対象はTOML/preset/lockパイプライン
  （`_compose_v2`系）で、legacy-yamlパイプライン（`_compose()`/`render_project()`、`apply`不可）は
  対象外と判明したため触っていない。新規`connection-targets@1` operator input（`operator_inputs.py`）、
  `_locked_connection_targets()`とruntime_config／Bridge allowlistへの配線（`render.py`）、
  `vps-server@4/profile.toml`へのoptional role追加、`lock.schema.json`のoneOf拡張、テスト追加。
  未指定時は既存出力と完全互換（回帰テストで確認済み）。259 test PASS、ruff clean。
- 捕捉cleanup（既存VPSの`Bridge stable`/`Bridge beta`がケータリング型以前の残骸か確認・除却）は
  knowledge側では追跡せず、この実装が固まった後のmc-remote-backstage専任セッションの作業として
  持ち出す。
- Scratch側とのconnection_targets突き合わせ検証は、home非公開alpha（m720s1、`home-server@2`）では
  できないと判明した。`home-server@2`は`renderer=compose@1`で`[[services]]`が`minecraft`単体のみ、
  Caddy/Scratch/Bridgeを持たない。運営者は当初alpha環境にScratchがある前提だったが、実際には
  セットアップ後に自分でテストしておらず、現状の内容を正確に把握していなかった。Scratch/Bridgeを
  含むのは`compose@2`〜`@4`（`vps-server@4`のみ現存）で、これは`exposure=public`/`bind_address=
  0.0.0.0`前提。DECISIONS `2026-07-25-03`（ケータリング型確立をrelease系より優先）を踏まえ、
  home-server向けにCaddy/Scratch/Bridgeを持つisolated/lan-only対応の新profile revisionを設計し、
  それをm720s1へ実際にセットアップ・動作確認するところから次回を始める。それに合わせてb3検収の
  live-human evidence（pair/token reconnect、接続先別token、設定先と実接続先の表示）も取得する。

## 2026-07-30 homepage / WordPress FPM設計骨子のknowledge着地待ち

- [ ] `docs/homepage-deployment-design_ja.md` §8〜§10へ、WordPress FPM templateの設計骨子を
  記録した。FPM variant、Caddy / FPMの同一document root、core read-only、`wp-content`
  persistentかつFPMだけread-write、WordPress外static subtreeのGit管理、numeric UID/GID検証、
  application rollbackとdata restoreの分離を確定範囲とする。
- [ ] 一方、official imageのexact tag / entrypoint、同一core treeの共有実装、exact UID/GID、
  secret projection、backup / restore、deployment分割はWordPress previewでの検証待ちである。
  長寿命volumeへdocument root全体を置いてimage管理coreを隠す方式は採らない。
- [ ] 確定搬送票は
  `handoff-materials/2026-07-30-wordpress-fpm-homepage-boundary/materials/confirmed-handoff_ja.md`
  に保存した。knowledge着地後に照合し、`[→DEC <ID>]`へ更新してhandoff materialをcleanupする。

## 2026-07-28 dev/alpha/実験環境の物理host分離とadmin access方針

- `dev` / `alpha` channelを別物理hostへ分離する運用へ移行する。従来は単一ラップトップが
  唯一のローカルリポだったが、`dev`用hostを新設し、これを主たるローカルリポ・commit/push元とする。
  `alpha`用hostは「trusted checkout」として扱い、そこでの直接編集・commitはせず、
  人間がテスト・Ruff green を確認したexact commitをfast-forwardで反映する（既存VPS運用と同型）。
  別に、自由に破壊実験してよい環境も用意し、そこのgit stateは正本として扱わない。
  具体的host名・IP・private inventoryは`mc-remote-backstage`側の管轄であり、本行には書かない。
- ラップトップは、貢献者PCの役割（fork/Issue/PR経由での関与を実地で試す用途）へ転用する案がある。
  外部貢献者が実際にたどる導線を運営者自身が検証できる。
- admin access（`dev` hostへのSSHなど）は、home router側でのSSH inbound port開放は避け、
  mesh VPN（Tailscale）を採用済み。全管理devicesへ導入し、古いiPadでも問題なく動作した。
  管理面の標準ツールとして確定する。tailnet参加端末同士は物理LAN内外を問わず同一private
  IP/MagicDNS名で到達でき、モバイル環境からの利用も場所を意識せず接続できる。
  tailnet未参加の同一LAN上の他デバイス（家族・来客等）には影響しない。
- 公開domainをLAN内から参照する際のhairpin NAT回避には、TailscaleのSplit DNS機能
  （tailnetスコープでdomain解決先を内部DNSリゾルバへ振り向ける）を採用する。バックエンドの内部DNS
  リゾルバは軽量なdnsmasqを使う（tailscale自体が権威応答するわけではない）。これにより従来の
  `/etc/hosts`手動書き換えが不要になる（場所・tailnet参加有無に依存しない解決）。ルーター/家庭内DNSでの
  override（LANスコープ限定）は見送った。ポート転送設定（一部portのみ外部公開）とは独立したレイヤーであり、
  互いに競合しない。

## 2026-07-28 通信方式とカリキュラムの関係（並列ビークル論の拡張）

- 「socket→WS→WSS→tunnel」を単一の難易度階段として教材化しない。direct TCPは、既存の
  `並列ビークル論`（Scratch/Python、20-教材`ai-learning-design_ja.md`）と整合する形で、
  Scratch側のWS/WSS/Bridge/Tunnelへ至る「踏み石」ではなく、それ自体で完結する独立した
  vehicleとして維持する方針。`2026-07-16-04`の「direct TCPをsocket入門の第一級transportとして
  維持」とも一致する。
- socket入門をMcRemote plugin本体で提供する必然性はなく、fork版や別pluginでの提供も
  選択肢に含めてよい。「並列ビークル」は単一codebaseでの実装を要求しておらず、vehicleの
  並立だけを指すため、この分離は既存原則と矛盾しない。
- 会話の中で、通信方式の学習設計（カリキュラム軸）とネットワーク公開手段の運用設計
  （インフラ軸）を混同しかけた場面があった。両者は独立した軸として扱う。

## 2026-07-29 Cloudflare Tunnelによるhome公開経路（分岐component検証・stable showcase用）

- 恒常的な公式public betaの代替ではなく、**分岐していくcomponent組み合わせの短命な実験公開**、および
  **stable版の複数variation showcase**向けに、Cloudflare Tunnelでのhome公開経路を確立する方針。
  近日中に検証する。
- 評価結果: home可用性は高くこれを理由に見送らない。inbound攻撃面はCloudflare Tunnelの構造上
  発生しない（listening portが無い）。残るのは事業者依存リスクのみで、これは許容範囲と判断した。
  レンタルVPS（XServer等）への依存とは性質が異なる点に留意する。VPS依存は「箱そのもの」を預ける依存
  （障害時は別VPSへの丸ごと移設が必要。ケータリング型のpreset/order/lockが移設容易性を担保）。
  Cloudflare依存は「経路」だけの依存（実体はhomeに残り、障害時は別経路——port forwardやSSH tunnel等
  ——へ切り替えるだけで済み、データ自体の移動は不要）。
- **背景**: Paper 26.2が安定版になったため追従が必要。自分たちの安定版リリース時は、26.2と
  1.21.11を並行維持することになる見込み（`2026-07-25-02`⑤のversion別instance＋Bridgeの
  `server_id→host:port` route mapで分離、Velocity/Gatewayは不要）。このhome公開経路は、
  複数stable variationを並行して外部showcaseする用途にも使える。

## 2026-07-28 学校でのケータリング型展開：接続方式の検討結果

- 生徒がMcRemote/Minecraftへ、教室外（帰宅後・長期休み中）からも継続してアクセスできる方式を広く検討した
  （Tailscale Funnel、Cloudflare Tunnel、port forward、VPS上への生徒専用instance）。結論として、
  既存の並列構成（教室完結のケータリング型＋別途VPS上の公式箱庭／公式world）を上回る決定的な代替案は
  見つからなかった。
- **唯一拾えた知見**: school networkがoutbound SSHを許可する場合、ケータリングPCからVPSへの
  reverse SSH tunnel（`ssh -R`、`autossh`等で常駐化、VPS側は転送専用の制限付きaccountを分離）が、
  Tailscale Funnel・無料Cloudflare Tunnelのどちらも運べないraw TCP（Minecraft本体プロトコル）を
  中継できる唯一の手段だった。VPS側はPaperを動かさず中継のみのため、最安tierのVPSで足りる。
  Minecraft世界の実体はケータリングPC側に置いたまま、VPSは公開到達点としてのみ機能する。
  ただしMinecraft世界に保存価値・所有権を持たせず使い捨て前提（定期リセット＋生徒コードから
  自動再構築）とするなら、ケータリングPC側とVPS側の世界を同期する必要はなく、VPS側に生徒専用の
  独立した使い捨てinstanceを別途用意する方がむしろ単純になる。
- **展開の枠組み（未言語化だった整理）**: 導入摩擦を下げるため、まず単体で完結し教師の管理が容易な
  scopeへあえて限定したケータリングPC構成で教室の内側に入る。教室外の継続利用は、需要（主に生徒からの
  要望）が先に立ってから、担当教員が校長へ提案し予算化・学校のIT委託業者による構築（必要なら本プロジェクトも
  支援）という別トラックで開く。教室外の選択肢としては、学校専用VPS（部活動等の限定利用になり得る）に加え、
  本プロジェクトの公式箱庭・公式worldも並行した選択肢となる。単一のtopologyで教室内・教室外の両方を
  最適化しようとしない、という整理。
- **未整理のまま残った項目**: 個人環境でのCloudflare Tunnelの具体的な用途、VPS-ホームサーバー間の
  SSH tunnel構成。いずれも別途検討が要る。

## 2026-07-27 catering VPS session handoff

### Git / deployment現在地

- 作業branchは`codex/catering-recovery-transactions`。Phase 1は
  `7f02d69 Add catering recovery transactions`、Phase 2は
  `fe74ce9 Refine live recovery diagnostics`、CLI検証環境の分担計画は
  `c2a08de Record CLI validation environment plan`である。push / PRはしていない。
- VPSのtrusted checkoutは`fe74ce9`、local checkoutは`c2a08de`でclean。後者は文書だけの差分で、
  現行runtimeを更新する理由にはしない。
- `official-public-beta`はlock
  `sha256:979a677b3fb9c83a1ed5e1704c60f3054bcc8ae3e758a8877df9134b502f4d56`、
  Minecraft 1.21.11、protocol 21.0.0でhealthy。doctorは
  `render=additional-compose-files`をWARNし、compatibilityは
  `public VPS catering transaction evidence is being established`を理由にunverifiedを維持する。
- Scratch betaからの接続、`postToChat`、`setBlocks`は人間確認済み。world 3 root、
  server icon、11 active plugin JARを最新beta backupから復元している。

### 現在の停止境界

- 現行plugin set / config / secretは一時的なrecovery Compose overrideで供給する。
  plugin compositionを正規order / lock / renderへ取り込むまで、通常`apply`、world restore apply、
  canonical migrationを現行VPSへ実行しない。
- 現行world volumeを実験用に使わず、recovery overrideを正規lockへ暗黙昇格しない。
- remote backup、local暗号文、transfer record、restore rollback directoryは、retention /
  finalize契約を確定するまで自動削除しない。
- 他repoを編集・commit・pushしない。本repoも人間の明示指示なしにpushしない。

### backup / restore現在地

- ServerBackupは00:30 / 04:30 / 08:30 / 12:30 / 16:30 / 20:30の1日6回、
  local plaintext最新6世代、plugin内蔵FTP無効。完成ZIPはsystemd timerからage暗号化し、
  explicit FTPS adapterでoff-host搬送する。
- 自動搬送、remote再download SHA-256、ZIP CRC、activation marker、stable-age、
  duplicate skipはlive PASS。実remoteには旧暗号文2件が`record=missing`、今回の自動搬送 /
  smoke暗号文2件が`record=present`として見える。
- record / ciphertext download、decrypt、archive inspect、world-only restore transactionは
  deterministic CLIとして実装済み。実FTPS世代からのlive world restore applyは未実施で、
  canonical plugin composition後に行う。
- local / remote暗号文のretention、legacy `record=missing`の扱い、成功後rollback directoryの
  manual rollback / finalizeは未決定・未実装である。

### plugin recovery現在地

- DiscordSRV secretはconfigured、Geyser / Floodgate / ServerBackup configは復元・調整済み。
  Floodgate keyは不要でabsent、LuckPerms DBはfreshのまま。plugin JAR / data / configは
  recovery overrideで供給し、正式なplugin data復元範囲は未批准である。
- plugin compositionでは、JAR identityに加えてconfig template / override、secret参照、
  data directory所有権、load順・依存、runtime download、update check、egress、
  backup / restore policyをlock / render / doctorへ投影する必要がある。

### 次の順序と人間判断

- 環境分担と安全な検証順は
  [`docs/cli-validation-environment-plan_ja.md`](docs/cli-validation-environment-plan_ja.md)を入口とする。
  まずlocalでplugin compositionとupgrade / reapply transactionを実装し、ケータリングPCで
  破壊的bootstrap / rollback / restore、home serverの別lab projectで長期統合を行う。
  現行VPSは稼働を維持し、最後に人間checkpoint付きcanonical migrationとpublic evidenceを行う。
- 人間判断待ちは、McRemoteオリジナル箱庭template、server icon規定のarchiveからの回収、
  local / remote backup retention、legacy暗号文、rollback directoryの削除時点、
  plugin dataの正式復元範囲、LuckPerms DB復元、compatibility evidenceの正式着地時点である。

## 2026-07-24 home-beta bootstrap後

- [ ] Paperのdefaultに依存しないMcRemoteオリジナルのserver templateを別sliceで設計する。
  汎用`minecraft-server@1`のtyped instance設定とは分離し、教材・公開体験向けにアレンジした
  「箱庭」のgameplay、world、performance、表示文、初期contentを再利用可能なpresetとして定義する。
- [ ] McRemote server iconの既存規定を現行public SSOTから回収する。今回確認した公開knowledgeの
  関連箇所では規定を特定できていないため、記憶だけで再定義せず、approved knowledge handoffで
  出典を回収してからformat / size、asset provenance、immutable identity、render / install契約を
  上記server templateの近接仕様として確定する。
- [x] `vps-server@4` / `public-web-paper@1`をVPS実機でbootstrapし、Caddy / Scratch /
  Bridge / Minecraft、internal app network、public bind、managed volume、token無し
  protocol helloを`live-auto`検証する。host / provider firewallとDNSは人間checkpointとして
  分離し、外部HTTPS / WSS readinessとhomepage artifactは後続claimとして記録する。
- [ ] operator CLI install / PATH契約を確定する。bootstrap期はcheckout内
  `.venv/bin/mcrctl`をexact pathで使う。既存Ubuntu環境とクリーン環境の両方で、
  `uv tool install`等の候補を比較してから公開runbookへ採る。
- [x] 実利用者のagent-assisted bootstrapをclean Ubuntu Server hostで検証した。
  human-runだけで完走可能なcommand経路を保ち、管理端末からSSHする支援経路では、
  SSH hardening、package導入、EULA、unverified理由、plan / lock review、applyを
  人間checkpointとして戻した。
- [ ] 対象host上agentは、重要dataを持たない再構築可能なhostと専用非特権userで限定実験する。
  `sudo` / rootful `docker` group / SSH agent forwarding / personal credentialを与えず、
  agent-owned scratchと管理者所有のtrusted checkout / projectを分離する。applyとdoctorを
  trusted側の別terminalへ戻した場合の実用性と学習価値を評価する。
- [ ] agent-assisted bootstrapの基準経路、管理端末からの支援経路、対象host上の実験境界、
  人間checkpoint、pause / handoff境界を
  knowledge owner向けsanitized draftとしてhandoffし、`20-教材/ai-learning-design_ja.md`と
  `00-hub/llm-agent-boundary-guide_ja.md`への着地確認をownerへ依頼する。
- [ ] Rootless Dockerを安全性の候補として別sliceで評価する。現行apply、managed volume、
  backup / restoreとの互換性を確認するまで、agent利用のためにdeploymentを切り替えない。
- [ ] token無しhello以外のread-only / reversibleなprotocol command smoke範囲を決める。
  world変更を伴うtestは、復元境界と人間の学習価値を確認してから実施する。
- [ ] pairing、Minecraft内command、実player操作を`live-human`として実施し、正式根拠に使う回は
  knowledge ownerへrecord + sanitized artifact draftをhandoffする。
- [x] `mcremote-paper@1`のrequired claimsとunit / live-auto結果を照合し、
  exact `home-server@2` subjectへcompatibility recordを追加した。bootstrap成功だけでなく、
  isolated runtimeのprotocol helloとlan-onlyのrender-only結果を主張境界付きで固定した。
- [ ] compatibility record追加前のunverified lockで稼働中のruntimeを、world volumeを維持したまま
  verified lockへ移すdeployed-state transactionを設計する。record追加はcandidate lock identityを
  変えるため、upgrade apply未実装の現状では対象host checkoutを単純更新・再resolveしない。
- [x] home private alphaを`home-server@2` / `mcremote-paper@2` /
  `alpha` / `isolated` / `integration`の別project・別volume・別world・別portでlive検証する。
  `mcremote-paper@2`はb2 exact artifactを使うdeployment-path検証用で、live evidence着地前は
  unverifiedを維持する。
- [ ] home private alphaのsanitized `live-auto`素材をknowledge ownerへhandoffし、
  正式evidence着地後にexact subjectのcompatibility recordを別変更で追加する。
- [ ] backup / restore、upgrade rollback、host-level multi-project collision、
  `lan-only` / firewall責任分界を独立sliceで検証する。
- [ ] official VPS betaのbackup / restore contractを現行TOML経路へ移す。2026-07-26観測では、
  `/var/lib/mc-remote/backup-beta/outbox`に毎日03:33のwhole-server ZIPが
  2026-07-21から2026-07-26まで6世代あり、各約786.5--787.0 MB、ownerはruntime UID/GID
  `10001:10001`だった。最新世代
  `backup-2026-07-26~03-33-00-..zip`はSHA-256
  `ec5dd15316e76036b8618c13278e802507c9ced102c24ab548fdb9bf0a94ea1c`、
  CRC OK、1525 entry、244 region、11 active plugin JARだった。world 3 rootとserver iconを
  current managed volumeへ復元し、Minecraft 1.21.11、11 plugin、doctor、public endpoint、
  Scratch betaからの接続・`postToChat`・`setBlocks`までlive確認した。
  外部生成元`/etc/mc-remote/generated/minecraft-beta/plugins/ServerBackup/config.yml`は
  `BackupDestination: /backup/outbox`、`DeleteOldBackups: 0`、`BackupLimiter: 0`、
  plugin FTP upload / local deleteともfalseだった。6世代・約4.7 GBは上限ではなく稼働日数の
  結果だった。復元後のbetaは00:30 / 04:30 / 08:30 / 12:30 / 16:30 / 20:30、
  `BackupLimiter: 6`、`BackupDestination: /backup/outbox`、plugin FTP無効で起動し、
  runtime UID/GID `1000:1000`からoutboxへ書込みできることを確認した。local plaintextは
  6世代となったが、adapter所有の暗号文 / transfer recordのretentionは別途決める。
- [ ] archive
  `mc-remote-knowledge-archive@54652a9e25c7535b637645a4b88a1543cc998006:
  40-サービス運用/server-package-design_ja.md`のbackup契約を回収候補として照合する。
  snapshot生成と外部転送を分離し、betaは03:33、producer outputは`/backup/outbox`、
  whole-server archiveはsecret-bearing、通常world restoreはcredential storeを上書きしない。
  archiveは現行SSOTへ未着地なので、そのまま実装根拠にせずknowledge ownerへ搬送する。
- [ ] off-host restore経路のlive smokeを完成させる。決定論的CLIにはage暗号化、
  explicit FTPS upload、remote size / download SHA-256検証、秘密を含まないtransfer record
  sidecarのatomic upload、明示remote list、record / ciphertext download、record照合、
  age復号と平文SHA-256照合を実装した。world-only restoreもunsafe / duplicate / symlink
  entry拒否、credential除外、current lock / render / managed volume束縛、staging、停止、
  cutover、start、doctor、失敗時rollback、成功時旧world保持まで実装した。実FTPS相手の
  sidecar付き往復は、最新betaと同じSHA-256のcopyをage暗号化し、explicit FTPSでatomic
  upload、remote再download SHA-256まで`download-verified`でPASSした。暗号文は
  787,157,433 bytes、SHA-256
  `744cedbc9febec5f42feeae75484e4af2b1286033851e847d03cbd0a21b776a4`。
  `backup drain`は明示activation markerより新しいarchiveだけを対象にし、120秒のstable age、
  ZIP CRC、検査中のidentity / size / mtime不変、`download-verified` recordによる重複防止を
  行う。systemd timerの初回はmarker以前の6世代を`none-ready`として除外し、手動で生成した
  新しいwhole-server ZIPだけを自動搬送した。暗号文は776,236,198 bytes、SHA-256
  `82eb7be298226b018bdac965c577e2fd398b2231245d57f02908487ce7c49100`、
  statusは`download-verified`。XServerはupload中の既知名`SIZE`を受理する一方、`NLST`後の
  `SIZE`を550にするため、remote listはsize factを持つ`MLSD`を優先し、非対応serverだけ
  `NLST` + `SIZE`へfallbackする。remote listは暗号文をsidecar有無とともに表示し、
  `record=missing`を復元可能とは扱わない。新CLIによるlive world restoreは未実施である。
  現行official public betaは一時的な`compose.recovery-plugins.yaml`を併用しているため、
  canonical composeだけで再起動するworld restore applyはplugin setを外す。restore preflightは
  追加Compose fileを検出して拒否する。doctorはruntime / protocol確認を続けながら
  `render=additional-compose-files`を警告し、applyも同じlockのno-opとは扱わずmutation前に拒否する。
  live applyはplugin compositionを正規lockへ取り込んだ後に行う。
  TOML deployment用off-host transportはprivate mode-0600 fileを明示指定し、provider /
  account inventoryをpublic project orderへ混ぜず、passwordは既存secret storeから参照する。
  ServerBackup 2.10.0の公式仕様では`DeleteOldBackups`は日数、`BackupLimiter`が総世代数である。
  official betaは毎日00:30 / 04:30 / 08:30 / 12:30 / 16:30 / 20:30、local最新6世代、
  producer内蔵FTPは無効、完成archiveをage + explicit FTPS adapterで毎回転送する。
  VPS homeには2026-07-13 beta候補と2026-07-14 stable候補の`.zip.age` /
  `.transfer.json`が残り、両recordとも`download-verified`だった。一方、
  2026-07-21--26のMinecraft 1.21.11 beta最新6世代については、home配下に暗号文 /
  transfer record / 現行transport configを確認できず、off-host転送済みか未判定だった。
  XServer側のaccount rootはFTPS session内の`/`として観測された。remoteには旧stable /
  beta候補の暗号文2件と今回の自動搬送 / smoke暗号文2件が見える。旧2件にはremote sidecarがなく、
  今回の2件にはsidecarがあるため、`backup list`はそれぞれ`record=missing` /
  `record=present`として区別する。
- [ ] plugin compositionを通常の非container運用に近い表現力へ上げる。JAR identityだけでなく、
  pluginごとのconfig template / override、secret注入、data directory所有権、load順・依存、
  runtime library / content download、update check、egress、backup / restore policyを
  lock / render / doctorへ投影する。今回の復元起動ではDirectionHUDがPaper libraryをMavenから
  取得し、GeyserがMinecraft JARを取得、ServerBackup / DiscordSRV / ViaVersionがupdate checkを
  行った。`archive inspect`はplugin descriptorのruntime library宣言をinventoryし、
  `runtime audit-log`は明示log eventをURL pathやraw lineなしで分類するが、network accessの
  不在証明やtransitive dependency lockではない。正式plugin setとplugin data復元範囲は
  判断待ちのため、現行recovery overrideを正規lockへ暗黙昇格しない。
- [x] WoL / WoWLANの重要性、非標準化の理由、検証境界を
  [`docs/wake-on-lan-field-note_ja.md`](docs/wake-on-lan-field-note_ja.md)へ公開した。
  WoLは準24時間運用で重視する一方、一般bootstrapの必須機能、profile capability、
  compatibility条件、b3開始gateにはせず、利用者の手元で再現不能なhardware条件を強制しない。
  異なるdesktop hardware 2台で、Python 3.12.3 / `wakeonlan 0.41`、directed broadcast、
  deep sleep / poweroffの相互8ケースとservice healthを確認済み。正式`live-human` evidenceは
  knowledge commit `4b8ab4b6e173053e4c9a167011d6ed0c8ae4bd1c`の
  `14-evidence/records/2026-07-25-ubuntu-desktop-wol-mutual-live-human_ja.md`へ着地した
  [→DEC 2026-07-25-08]。
- [ ] WoL / WoWLANのhardware-specificな外部技術記事化を続ける。
  同一hardwareのWindows Wi-Fi poweroff復帰はoperator観測あり、Ubuntu WoWLANは未成立として、
  firmware / NIC・Wi-Fi chipset / OS / kernel / driver / renderer / power stateを固定した事例にする。
  `Restore on AC Power Loss`やwatchdogをWoLの成功主張へ混ぜない。
- [ ] ケータリングキットのnetwork topologyは既存Ethernet接続hardware APを最初の検証routeとし、
  Wi-Fi-to-Ethernet bridge、内蔵WoWLAN、USB Wi-Fi / USB Ethernet、専用router PCを比較する。
  USB常時給電だけでremote wake可能とはみなさず、exact USB ID / chipset / driver、
  USB remote wake、broadcast透過、offline DHCP / DNS、client isolation、設定restoreを実機確認する。

公開可能な現時点の観測:

- 既存Ubuntu Desktop hostを再インストールせず、`home-server@2` /
  `mcremote-paper@1`のisolated `home-beta`を実機検証した。
- repo tests / Ruff、order / lock validate、repo check、canonical plan、
  healthy container、同一lockのno-op apply、protocol `21.0.0` helloがPASSした。
- `0c63076`を既存hostへfast-forwardし、対象host上でも202 tests / RuffがPASSした。
  exact checkout pathの`mcrctl doctor`でcurrent lock / canonical render、managed volume /
  healthy container、loopback限定port、protocol `21.0.0` / Minecraft `1.21.11` helloがPASSした。
  旧版projectのroot / tracked inputは人間が明示して`0750` / `0640`へ締めた。
- 管理端末上のagentがSSHするmodeで1台目を検証し、PATH前提、TCP LF quoting、
  private/public handoff分離の改善点を回収した。対象host上agentは正規modeとはせず未検証。
- clean Ubuntu Server 24.04 hostを、既存container / volume / imageなしの状態から
  管理端末上agent + SSH modeでbootstrapした。checkout `3589d35`で対象host上の
  205 tests / Ruff、exact artifact fetch、canonical renderのno-op再実行、review済みlockでの
  bootstrap apply、doctorのhealthy runtime / current render / loopback限定port /
  protocol `21.0.0` / Minecraft `1.21.11` helloがPASSした。
- clean bootstrapで、cloud-initの`50-cloud-init.conf`より後のSSH drop-inでは
  password loginを無効化できないことと、新規lockが`0644`になる不具合を発見した。
  SSH drop-inを先行評価させるrunbook修正と、新規lockを最大`0640`かつcaller umaskを
  緩めず作る実装修正を、それぞれ回帰テスト付きで反映した。
- 既存個人管理者userはrootful Dockerを操作できるため、安全側のオンホスト実験profileに
  合わない。1台目へagentをinstallせず、再構築可能な別hostで評価する。
- exact `home-server@2` + `mcremote-paper@1`はcompatibility record追加後の新規resolveで
  `verified`となり、unverified acknowledgementは不要になった。record追加前のlockで稼働するhostは
  migration transactionができるまで旧checkout / lockを維持する。private inventoryとraw logは
  本repoへ置かない。
- exact `home-server@2` + `mcremote-paper@2`のhome private alphaを、既存betaと別project /
  volume / world / loopback portでbootstrapした。対象checkoutで208 tests / Ruff、初回apply、
  同一lockのno-op apply、doctorのhealthy runtime / current render、protocol `21.0.0` /
  Minecraft `1.21.11` helloがPASSし、既存betaもhealthy / currentを維持した。正式evidence着地前の
  ためalpha presetは意図どおり`unverified`のままとする。
- 異なるdesktop hardware 2台を相互sender / targetとし、Python 3.12.3と
  `wakeonlan 0.41`からdirected broadcastへmagic packetを送った。両方向のdeep sleep /
  poweroff、boot ID、SSH、1または2 containersのhealthを8ケースすべて確認した。
  poweroffではSSH不能が完全消灯より先行するhostがあり、人間の完全消灯確認後に送信する
  二段checkpointが必要だった。MAC、IP、private host名は公開記録へ含めない。
