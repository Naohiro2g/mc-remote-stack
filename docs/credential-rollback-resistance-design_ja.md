# Long-lived credential の rollback resistance 設計

文書状態: stack側の設計・実装投影。横断契約はknowledge SSOTの`2026-08-02-01`へ着地確認済みで
ある。McRemote単体実装とstackのdeterministic write-set実装は完了したが、cross-repo live evidenceと
公開既定化は未完了である。実装gateは`2026-08-02-03`を参照する。

- 作成日: 2026-08-02
- knowledge確認commit: `58301cfa6d6ed998a8a6e38cdae1e8f7aa512abb`
- knowledge着地commit: `9dd99f3aecccf4035cdfd5549d1e70f9f25d3b2d`
- decision: `2026-08-02-01`（確定）/ `2026-08-02-03`（公開導線gate・保留）
- plugin b3 release source commit: `a3dab998b710f65f42f95058a68ec51d419b097c`
- plugin b3 release evidence commit: `3dfbf57c07f2b7985c65edc5564b879f9e67e122`
- 入力: 2026-08-01 long-lived credential lifecycle搬送票2

## 1. 解くべき不変条件

保証対象は「credential snapshotを過去へ戻さない」という運用禁止ではない。次を満たす。

> revoke成功を応答したlong-lived credentialは、rollback可能なcredential snapshotがrevoke前へ
> 戻っても、再び認証成功してはならない。

McRemote `a3dab998b710f65f42f95058a68ec51d419b097c`では、`mcrl_`、永続store、credential list /
revoke / logout、`RevocationAuthority`に加え、b3 catalogとLuckPerms effective build range修正が
実装済みである。tag `v1.21.11-2100.0.0b3`のGitHub prerelease asset
`mc-remote-1.21.11-2100.0.0b3.jar`はsize `138178`、SHA-256
`aeb190705bd9957ce73557dc1be0fe15efe7250ba9bc688945e6f537e00ef78e`である。Stack自身のartifact
fetcherで公開assetを再取得し、このdigestとの一致を確認した。bundled `mcremote-paper@3`はこのassetを
exact-pinするが、compatibilityはlive evidenceが揃うまで`unverified`を維持する。

この保証は、Minecraft world restoreやcredential snapshot restoreを誤って実行するoperatorから
守る運用安全性である。host rootや同一JVMの悪意あるpluginがauthorityを改ざん・削除する攻撃までを
filesystem配置だけで防ぐとは主張しない。rollback対象がauthority自体を含む場合、外部の単調な状態
なしに過去と現在を区別することはできない。

## 2. 採用する状態分離

一つのatomic snapshot fileだけを認証の正本にしない。次の二つへ分ける。

| state | 内容 | rollback policy |
| --- | --- | --- |
| credential snapshot | 発行record、device、時刻、`revoked_at`を含む検索・管理用snapshot | recovery pointへの明示restore可 |
| revocation authority | credential domainと、revoke済み`credential_id` / `token_hash` / `player_uuid` / `revoked_at`のcreate-only tombstone | credential snapshotと同時にrollbackしない |

初期実装は`CredentialStore`と`RevocationAuthority`を別interfaceにする。前者はatomic snapshot
file、後者は一tombstone一fileのcreate-only directoryを第一候補とする。append-only log一個に
するとtorn tail、compaction、並行appendの回復規則が増えるため、v1では採らない。

authorityはcredential内容の秘密正本ではないが、mode `0700` directory / `0600` fileとし、
通常log・公開evidenceへtoken hash、credential ID、player UUIDを出さない。

### 2.1 credential domainと保存形式の最低要件

authority初期化時にランダムな`credential_domain_id`を生成する。recordが0件でもdomainを検証できる
よう、IDは各recordだけでなくsnapshot headerとauthority manifestに必須とする。これはwire fieldでは
なく、token本体にもencodeしない。最低限の内部形は次のとおりとする。

```json
{
  "schema_version": 1,
  "credential_domain_id": "...",
  "records": []
}
```

```json
{
  "schema_version": 1,
  "credential_domain_id": "..."
}
```

tombstoneにもdomainを保持する。

```json
{
  "schema_version": 1,
  "credential_domain_id": "...",
  "credential_id": "...",
  "token_hash": "...",
  "player_uuid": "...",
  "revoked_at": "..."
}
```

recordへdomainを重複保持する実装では、全recordがsnapshot headerと一致することをload時に検証し、
不一致をcorruptionとしてfail closedにする。未知のschema versionも空store扱いにしない。

- 新しいdomainを作れるのは明示的なbootstrap / reset transactionだけとし、plugin startupは
  authorityとsnapshotがともに無い場合も空stateを自動生成しない。
- stackはprofileに従って二backendの保存resourceを用意・mountし、明示bootstrap / resetのoperator承認と
  transaction管理を担う。domain ID、manifest、snapshot、tombstoneの形式と生成はpluginが正本であり、
  stackはplugin内部JSONを独自生成しない。
- 初回bootstrapではstackが同じapply transactionでprofile指定の保存resourceを用意・mountし、双方が
  空であることを確認したうえで、plugin所有の明示初期化surfaceを呼ぶ。
- 二backend stateを同時にatomic commitできないため、途中失敗ではdomain欠落・不一致として通常起動をfail
  closedにする。bootstrap再試行は、双方が空か、plugin所有の同一bootstrap transaction markerから
  同じ初期化の途中状態だと検証でき、かつcredential record / tombstoneがまだ無い場合だけ許可する。
- authorityが無いのにsnapshotが存在する場合、空authorityを自動生成せずfail closedにする。
- authorityとsnapshotのdomainが不一致ならfail closedにする。
- host全損で最新authorityを回収できない場合、明示resetで新domain＋空snapshotを作り、全credentialを
  失効させて再pairする。古いsnapshotを新domainへ昇格しない。
- resetは通常起動の自動修復に使わない。全credential失効をoperatorへ明示した管理操作だけとし、
  bootstrapと同様にplugin所有の形式をstackが生成しない。

これにより、authorityを持たない別environmentや再構築hostへsnapshotだけをcopyしてもcredentialは
有効にならない。

## 3. 認証・発行・revoke transaction

### 3.1 認証

long-lived credentialは次をすべて満たす場合だけ有効とする。

1. snapshotとauthorityが読め、domainが一致する。
2. `SHA-256(raw token)`に一致するactive recordがsnapshotに存在する。
3. 同じtoken hashまたはcredential IDのtombstoneがauthorityに存在しない。
4. record自体の`revoked_at`がnullである。

authorityにtombstoneがあれば、rollbackしたsnapshotがactive recordを返しても常に
`token_revoked`とする。authorityを読めない場合は`credential_store_unavailable`でfail closedにし、
enforcementをOFFへ落とさない。

### 3.2 発行

1. 256 bit tokenとcredential IDを生成する。
2. current domain付きrecordをcandidate snapshotへ追加する。
3. temporary file write、file `fsync`、atomic replace、directory `fsync`を完了する。
4. 完了後にだけ生tokenを`auth.pairPoll`へ返す。

発行後のsnapshot rollbackはcredentialを消す方向に働き、未発行credentialを復活させないため、
revocation authorityへのwriteは不要である。crashで保存済みtokenをclientへ返せなかった場合は、
到達不能なactive recordとして上限を消費し得るため、管理診断と明示revokeで回収できるようにする。

### 3.3 revoke / logout

1. credential IDからtoken hashと所有UUIDをcurrent snapshot、または既存tombstoneで解決し、要求元UUIDが
   所有者であることを検証する。
2. authorityと同じdirectoryのcreate-only temporary fileへtombstoneを書き切る。
3. tombstone temporary fileの`fsync`を完了する。
4. finalへの非上書きpublishとauthority directoryの`fsync`を完了する。この完了をrevokeの
   **線形化点**とし、ここからcredentialは失効済みである。
5. 対象credentialの既存RemoteSessionを終了対象としてmarkする。要求元session自身が対象なら、
   responseを送るまではcloseしない。
6. 管理用projectionであるsnapshotの`revoked_at`を更新し、atomic snapshot writeを試みる。
7. revoke成功応答を返す。
8. 対象sessionのsocketを閉じる。別credentialで要求したsessionは維持する。

`credential_store_unavailable`を返すのは、step 4のauthority durable commit前に失敗した場合だけとする。
step 4以降にsnapshot更新が失敗してもrevokeの結果を失敗へ戻さず、server healthをdegradedにして
reconcile対象へ送り、revoke成功として扱う。これによりclient側の「このreasonではtokenを温存し、
再pairしない」という契約と衝突しない。

非上書きpublish後、directory `fsync`前後のI/O errorでは、callerからdurabilityを確定できない場合が
ある。このとき`credential_store_unavailable`は「tokenがactiveと確定した」という意味ではない。
authorityをunhealthyとして認証をfail closedにし、clientはtokenを温存したまま再試行またはhealth回復を
待つ。既存finalが正当ならdirectory `fsync`を再試行してcommitへ進め、不在なら未成立として扱う。

step 4直後のcrashや成功responseの喪失では、clientが結果を受け取れないままcredentialが失効済みに
なることがある。これはdurable commitとresponseをatomicにできないため避けられない。再試行は既存
tombstoneを検証してidempotent successとし、旧tokenで再接続した場合は`token_revoked`へ収束させる。
off-host durabilityを継続性の条件にするprofileでは、step 4のauthority durable commitにremote側の
durable acknowledgementも含め、全commit完了前を線形化点にしない。

list / limit計算 / `current`判定もauthorityをoverlayし、rollback snapshot内のrevoke済みrecordを
active件数や応答へ含めない。authority tombstoneはplayer UUIDも保持するため、snapshotが当該recordを
失った後でも、同じUUIDから同じcredential IDへのrevokeはidempotentな成功、他UUIDからは
`credential_not_found`として処理できる。UUID自体はwireへ返さない。

### 3.4 create-only file backend契約

「一tombstone一file」だけでは非上書き性とcrash durabilityを保証しない。初期file backendは次を満たす。

- final filenameはserver生成UUIDをcanonical表現へ検証した値だけから組み立てる。wireで受けた
  credential IDをpathや相対pathとして直接使用しない。
- authority directoryと同じfilesystem / directoryに、衝突しないtemporary fileを`CREATE_NEW`で作る。
  symlinkを辿らず、既存regular file以外を正当なtombstoneとして扱わない。
- 完全な内容を書いてtemporary fileを`fsync`し、finalへ原子的な非上書きpublishを行い、authority
  directoryを`fsync`する。対象platformで「既存finalを置換しない」ことを保証するprimitiveを選び、
  実装testで実証する。`REPLACE_EXISTING`相当は使用しない。
- finalが既に存在する場合は内容全体を検証する。同じschema / domain / credential ID / token hash /
  player UUIDなら、必要なdirectory `fsync`を完了した後にidempotent successとする。少なくともこれらの
  いずれかが異なればauthority corruptionとしてfail closedにする。`revoked_at`の差を上書きで解消しない。
- 中断されたtemporary fileはfinal tombstoneと名前空間・形式で区別し、認証判断へ使わない。cleanupは
  finalの作成や置換とみなさず、finalと同一性を確認してから行う。
- 壊れたfinal、未知schema、symlink、非regular fileを無視して起動しない。authority load時に全finalを
  検証し、credential IDとtoken hashの重複・矛盾も検査する。

このbackend契約はserializationをwireへ固定するものではない。SQLite等へ交換する場合も、線形化点、
create-only / idempotency、domain整合、durable-before-successと同じ意味保証を満たす。

## 4. recovery別の結果

| 操作 | credential snapshot | revocation authority | 結果 |
| --- | --- | --- | --- |
| `mcrctl world restore` | 変更しない | 変更しない | credential lifecycleに影響なし |
| credential snapshot restore | 過去へ戻り得る | currentを維持 | revoke済みtokenはauthorityが拒否 |
| environment clone | copyされ得る | 新domain | 全旧credential無効 |
| host全損、current authority回収可 | 復元可 | currentを復元 | authority overlay後に継続可 |
| host全損、current authority回収不可 | 信頼しない | 新domain | 全失効・再pair |
| authority unavailable / corrupt | 読めない | 読めない | auth fail closed、operator診断 |

「current authority回収可」と主張するには、authorityの保存先自体が対象rollbackより強い必要がある。
offlineのケータリングPCでは外部serviceを必須にせず、world rollbackだけをlocal分離で守り、host全損は
全失効・再pairを既定にする。VPSでcredential継続が必要なら、step 4の線形化点より前にoff-host authority
のdurable commitまで同期完了させる別ops契約が必要である。非同期複製だけではhost全損直前のrevokeを
失い得るため継続性を主張しない。remote freshnessを証明できない復旧は新domain＋全失効へ倒す。
whole-server archiveへsnapshotとauthorityを一緒に入れて同じ世代へ戻す方式は採らない。

## 5. stackへの投影

stackが保証する正本は物理volume数ではなく、保護対象rollbackのwrite setからauthorityを外すことである。
同じfilesystem上の非重複pathでも、restoreがsnapshotだけを書き戻すなら別rollback domainになる。逆に
別volumeでもVM / storage snapshotで同時に戻せばrevoke維持を保証できない。profileは保護できるrollback
範囲を宣言し、plugin単独で物理分離強度を推定させない。

少なくとも次の二つの論理backend roleを独立して設定できるようにする。

- `credential-store`: runtime data。pluginのatomic snapshot用。
- `credential-revocations`: security state。domain manifestとcreate-only tombstone用。

containerの既定profileでは、事故時のwrite setを明瞭にするため、二roleを`/data`外の別volumeへ置く
構成を推奨する。ただし、これはpluginの起動不変条件ではない。profileが同一filesystemを選ぶ場合も、
同一canonical path、一方が他方の配下、判定可能な同一backend identityを拒否し、restore / backupの
write setがauthorityを含まないことをstack側で検証する。

ServerBackupの`@server`は現在`/data`だけをarchive化する。credential用backendをwhole-server archiveへ
暗黙に含めない。credential snapshotをbackupする場合は専用exportとし、revocation authorityを同じ
restore transactionへ含めない。archive非包含は物理volume数から推測せず、render結果とarchive内容の
回帰・live testで確認する。

stackは保存resource lifecycleとoperator transactionだけを管理し、plugin所有のmanifest / snapshot /
tombstone JSONを書かない。bootstrap / resetではpluginの明示管理surfaceを呼び、途中失敗した二backend
stateを通常起動時に空stateへ自動修復しない。

現行`home-server@2` / renderer `compose@1`は`minecraft-data`一volumeだけを要求するため、そのまま
credential backend roleを表現できない。既存profileを破壊的に変更せず、`home-server@3` /
renderer `compose@5`を追加した。新revisionは`minecraft-data`、`credential-store`、
`credential-revocations`を別volume identityとしてlockへ固定し、後二者をそれぞれ
`/mcremote/credential-store`、`/mcremote/credential-revocations`へmountする。plugin設定には絶対path
だけを生成し、credential record、domain ID、manifest、tombstoneはorder / lockへ入れない。volume kindは
snapshotを`runtime-data`、authorityを`security-state`として区別し、将来の汎用backup選択でも両者を
暗黙に同じ扱いへ戻さない。

`compose@5`のrender、mount topology doctor、world-only restore試験は実装済みである。append-onlyな
`mcremote-paper@3`は公開b3 JARをexact-pinし、`credential-rollback-separated` capabilityを要求するため、
`home-server@3`だけでisolated alphaのresolver / fetch / renderへ進める。compatibilityは`unverified`で、
両backendが空のfresh stateだけを対象とするcontrolled live testでは、既存の理由付きacknowledgementと
review済みlockを要求してinitial applyを許可する。plugin所有console commandによるbootstrapと人間観測を
検証時だけ使用し、command出力を恒久契約にしない。機械可読credential health consumerが未実装の間は
通常doctorと一般運用のapply contractを開かず、resetもStackから呼ばない。`home-server@3`はalpha統合試験で
revoke結果を実際に強制するため、生成するMcRemote設定の`auth.enforcement`を`true`とする。既存preset /
profileのimmutable bytesは変更しない。b2の認証強制漏れを修正する通常profileは、append-onlyな
`home-server@4` / `vps-server@5`として分離する。

非container Paperでも二backend pathを明示設定する。同一canonical path、一方が他方の配下になる設定、
相対path、plugin data folderへの暗黙fallback、store障害時のin-memory fallbackはproduction authで
受理しない。同じfilesystem上の兄弟directoryはprofileが保護範囲を限定して宣言する場合に許容できる。

`mcrctl secret`はoperator secret material用のlocal storeであり、pluginが高頻度に追記するmutable
authorityではない。revocation authorityの置き場として再利用しない。また、本方式は公開鍵、nonce、
request signatureを追加せず、PoP parkを解除しない。

## 6. 共有されたlong-lived lifecycleとの整合

| 既存提案 | 結果 |
| --- | --- |
| `mcrl_`、`token_type: long_lived` | 変更なし |
| token hashは256 bit tokenのSHA-256 | 変更なし。authorityも同じhashで照合可 |
| UUID束縛、LuckPerms認可 | 変更なし |
| 個別list / revoke / logout | 維持。global epoch方式と違い他deviceを失効させない |
| 管理method | `auth.listCredentials` / `auth.revoke` / `auth.logout`を使用 |
| revoked recordをtombstone保持 | snapshot内に維持し、外部authorityをsecurity正本として追加 |
| atomic snapshot store / backend交換境界 | 維持。`RevocationAuthority`境界を追加 |
| `credential_store_unavailable` | 起動・read時の不健全、またはrevoke線形化点前のauthority commit失敗に使用。線形化後のsnapshot projection失敗には返さない |
| max active 16、自動失効なし | authority overlay後のactive件数で計算 |
| PoP / signature key lifecycleはpark | 変更なし |
| mcrp_ server migrationなし | 変更なし |

## 7. 却下した案

1. **world restoreでplugin directoryを除外するだけ**: unmanaged restoreやcredential snapshot restoreに
   対するREVOKE不変条件にならない。
2. **一つのglobal epochをrevokeごとに進める**: 一件の個別revokeで全deviceを失効させ、管理wireと
   UXに衝突する。
3. **snapshot digest / revisionだけを別fileへ置く**: rollback検出はできるが、二file commitのcrash
   recovery、A/B snapshot、pending commit規則が必要になる。REVOKE不変条件にはcreate-only
   tombstoneの方が小さい。
4. **HMAC / pepperでtoken hashを置換する**: signedな古いsnapshotは依然としてrollback可能であり、
   単独ではrevoke復活を防がない。secret注入とrotation lifecycleも増える。
5. **revoke時にglobal検証鍵をrotateする**: 全credentialを失効させ、個別revoke要件に反する。
6. **短い自動expiryへ変更する**: 「明示revokeまで有効」というlong-lived意味論を変え、復活可能な
   時間を短くするだけで不変条件を満たさない。
7. **cloud authorityを全deploymentで必須にする**: offlineケータリングを壊す。host全損時の
   全失効・再pairで安全に閉じられるため、v1必須条件にしない。

## 8. 実装sliceと回帰試験

### plugin

- `CredentialStore` / `RevocationAuthority` interfaceとfile backend
- snapshot header / authority manifest / tombstoneのdomain一致。空snapshotでも検証できること
- initial domain bootstrap、missing / mismatch / corruption fail-close
- plugin所有のexplicit first-bootstrap / resetだけが空stateを作り、stackがJSONを生成せず、plugin
  startupは自動初期化しないこと
- 二backend bootstrapの途中失敗をfail closedにし、双方空または同一transactionの安全な途中状態だけを
  再試行できること
- issue durable-before-return
- revoke tombstoneのfile / directory `fsync`を線形化点とするdurable-before-success、idempotent retry、
  session closure
- 線形化点前の失敗は`credential_store_unavailable`とし、publish / directory `fsync`境界の不確定時は
  authorityをunhealthyとしてfail closedにすること。clientはreasonだけでtokenを削除・再pairしないこと
- 線形化点後のsnapshot write失敗はsuccess + degraded health + reconcileで、auth / list / limit /
  currentへ即時overlayされること
- 線形化点直後のcrash / response喪失後も再試行または再接続でrevokedへ収束すること
- create-only publish競合、同一final再試行、内容矛盾、壊れたfinal、temporary file、symlink、
  duplicate credential ID / token hashを検証すること
- auth / list / limitへのauthority overlay
- store snapshotだけをrevoke前へ戻した再起動test
- authority欠落、domain mismatch、tombstone破損、snapshot write failure test
- raw token / token hashを通常logへ出さないtest

### stack

- 現行deterministic evidenceとして、world restore archiveにcredential fixtureがあってもhelperへ渡す
  展開・cutover対象が`world` / `world_nether` / `world_the_end`だけであることを維持
- 現行deterministic evidenceとして、recovery archive importがlock指定JARだけをcontent-addressed
  artifact storeへ書き、credential fixtureを取り込まないことを維持
- 新profile / renderer revisionに二backend roleと独立した保存resource設定を追加
- 既定profileでは二roleを`/data`外の別volumeへmountする推奨構成を用意するが、plugin起動条件にはしない
- 同一canonical path、包含関係、判定可能な同一backend identityを拒否する
- profileごとに保護対象rollbackとrestore / backup write set、物理分離強度を宣言する
- backup archiveにcredential backendが含まれないことをrender / live testで確認
- doctorでpath、mount topology、domain/store health、profileが宣言する保護範囲の非秘密statusを確認
- credential snapshot restoreはauthorityをmountし直さず、restore後にplugin healthを確認

Stack実装済みなのはprofile / renderer、三volumeのexact mount照合、world restoreとrecovery archive
importのwrite-set回帰までである。domain / store healthは未実装であり、McRemoteがplugin内部保存形式とは
別に、安定した機械可読の非秘密ops projectionを提供してから接続する。projectionは少なくともschema、
現実装の`UNINITIALIZED` / `HEALTHY` / `DEGRADED` / `UNHEALTHY`、snapshot / authorityの存在とdomain
一致判定を表し、domain ID、credential ID、token hash、player UUIDを出力しない。stackはmanifest JSONを
独自に解析・生成せず、このprojectionを通じてplugin自身の検証結果だけを読む。projection自体を取得・
検証できない場合はStack doctor側の`unavailable` errorとして区別する。

### live

1. `mcrl_`発行、正常停止・再起動後hello成功。
2. credential A / Bを発行しAだけrevoke。
3. snapshotをrevoke前へrollbackし、authorityはcurrentのまま再起動。
4. Aは`token_revoked`、Bは成功、list / active limitにAが現れないことを確認。
5. authorityを外した起動が空storeへfallbackせずfail closedになることを確認。
6. 新domain reset後はA / Bとも無効で、再pair後のCだけ成功することを確認。
