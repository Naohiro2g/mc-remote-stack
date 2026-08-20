# Deployment operator workflow redesign

公開beta更新を、releaseごとの救済migrationと人間によるshell編集の連鎖から、一つの反復可能な
operator transactionへ移す。これは既存`public-b3`／`public-b4`実装を一般化したと主張する文書ではない。
今後の通常経路、移行順、合格条件を固定し、未実装部分を明示する。

- 状態: operator environment、same-volume汎用update、runtime composition canonicalizationを実装
- Stack側根拠: b2〜b4のpublic beta実施で発生したtool不足、root所有化、artifact／Compose
  provenance／credential／runtime config／WireScope routeの逐次手戻り
- 利用者価値の錨: knowledge `2026-08-18-01`
- 適用先: public VPS、home beta／alpha、将来のcatering host

## 1. 変える単位

従来は「不足commandを別commandで代用」「Docker権限が無ければCLI全体をsudo」「releaseごとに
専用migration」「失敗するたび次の引数を追加」という局所解を採った。その結果、runbookを最後まで
読んでも一回の更新を予測できず、実施中の会話履歴が事実上の実行エンジンになった。

今後は次の三つを一つのdeployment productとして扱う。

1. operator environment: 必須tool、Python、Docker／Compose、直接Docker access、project owner
2. desired transaction: source runtime、target profile／preset、artifact、route、volume policy
3. verification: mutation前preflight、起動後doctor、失敗時の限定rollback、sanitized summary

通常更新に`public-bN`というrelease名のsubcommandを増やさない。`migration public-b3`／
`migration public-b4`は、その当時の非canonical runtimeを救出するhistory-bound commandとして凍結し、
新しいreleaseの通常runbookから呼ばない。

## 2. operator environment

信頼された人間operatorとagent userを分ける。公開VPSの個人管理者は既にsudo authorityを持つため、
Docker groupのroot相当権限を理解したうえで、その個人だけへdirect Docker accessを与える。
agent専用user、教材利用者、server利用者へは与えない。

`tools/bootstrap-ubuntu-operator.sh`がUbuntu hostの唯一の準備入口である。

- `--check`: 変更せず不足を一括報告
- `--install`: Git、固定uv、Python 3.11、Docker Engine、Compose 2.33.1以上を準備
- `--repair-project <exact path>`: 歴史的なroot実行で壊れた一project treeのownerだけを修復
- `--repair-artifact-store <exact path>`: 同じ原因で壊れた既定artifact storeだけを修復

準備後は`mcrctl operator check`が、非root UID、project owner、local Docker context、daemon、Composeを
一括検証する。project treeと宣言済みartifact storeも再帰検査し、root所有のnested fileを開始前に検出する。
全project commandはroot実行を拒否する。order、lock、generated、transaction state、artifact bytesを
同じoperatorが所有し続ける。

## 3. 守るもの

既定の保護対象は、保存済みScratch／Python建築コードである。Minecraft world、接続、pairing、
session、build state、WireScope状態は、個別契約が無い限り再生成可能なruntime stateとする。

したがって通常更新は、次を既定にしない。

- release番号ごとの新world volume作成と全byte copy
- session／pairing継続のためのdowngrade互換
- world変更を完全に打ち消すapplication rollback
- 古い教材記法を無期限に維持するshim

world保存やlong-lived credential継続が必要なenvironmentは、明示した別policyを選ぶ。追加手当は
「何を救うか」「手当が無いと誰が何に困るか」をplanへ表示できる場合だけ入れる。

## 4. 一つの通常入口

目標CLIは次の二段だけとする。

```text
mcrctl deployment update plan \
  --project <project> \
  --to-profile <profile@revision> \
  --to-preset <preset@revision> \
  --docker-context default

mcrctl deployment update apply \
  --project <project> \
  --plan-id <generated-plan-id> \
  --yes
```

`plan`はsource／target identity、artifact、実効Compose、route、volume policy、停止時間見積り、
human checkpointを一つのdurable planへ保存する。人間が複数のSHA、container ID、Compose path、
volume名を会話から転記しない。`apply`はreview済みplan IDだけを受け、入力の再解決や暗黙補完をしない。

target profile／presetの指定はrelease channelのdesired stateであり、release固有のPython function名では
ない。b5、b6でも同じ入口を使う。

## 5. 停止前preflight

planはmutation前に次を可能な限り一巡し、単発の最初のエラーだけでなくblocker一覧を返す。

- operator environmentと全project pathのowner／mode
- source order、lock、render、runtime Compose provenance
- target order、lock、canonical renderと全artifactのcontent-addressed store存在
- source／targetの実効Compose service、mount、port、network差分
- preserved overlayのexact digestと対象service
- credential health checkpointの要否
- Scratch runtime configのrequired field、default target包含、WireScope URL
- public route、配信対象artifact、内部health endpoint
- profileが要求する場合だけDNS／TLS／外部HTTP readiness
- rollbackに必要な旧order、lock、render、artifactの存在

exact HTTPS artifactのcontent-addressed store取得は、planの明示progressとして停止前に行う。
operator inputは`--set-input ROLE.KEY=VALUE`でcandidateだけへ型付き変更し、target adapterとschemaを
resolve時に検証する。live orderへ黙って書かず、apply時にreview済みcandidateをまとめてpublishする。
その他の不足は`NEXT` actionを機械可読に列挙し、同じplan commandを再実行する。runtime停止後に初めて
artifact欠落やroute欠落を発見する経路を合格させない。

## 6. applyとrollback

通常更新は既存managed volumeをそのまま使い、container／read-only artifact／generated configを
更新する。新volumeと全copyは、schema migrationや明示的な保存policyが要求する場合だけ使う。

applyは次をdurable phaseとして記録する。

1. reviewed plan再検証
2. target image pullとartifact再hash
3. source container停止
4. target desired stateのatomic publish
5. target起動とhealth待ち
6. doctorと外部claim
7. complete summary

失敗時は旧order／lock／renderと旧artifactでcontainerを再起動できる範囲だけrollbackする。world、
session、pairing、接続を完全復元したとは主張しない。旧runtime再起動にも失敗した場合はphaseとexact
next actionを残して停止する。source volumeの無条件copyや削除をrollbackの定義にしない。

## 7. Compose overlayを通常状態に残さない

recovery pluginやhomepageを追加Compose fileのまま恒久運用すると、毎回live container labelから
pathと順序を復元する必要がある。これは一度だけcanonicalization transactionで解消する。

- 継続するplugin／homepageはtyped operator inputまたはprofile componentとしてlockへ入れる
- secretはsecret store参照、公開artifactはcontent-addressed artifactとして固定する
- canonical render一式だけで同じruntimeを再構築できることをdoctorする
- canonicalization完了後は`render=additional-compose-files`を通常のWARNとして放置しない

未知overlayを自動採用しない。採用、廃止、別管理のいずれかを人間が一度決め、その後のrelease更新から
provenance復元作業を除去する。

現行public beta向けには、一回限りの次の入口を使う。

```text
mcrctl deployment composition plan \
  --project <project> \
  --to-profile vps-server@10 \
  --to-preset public-web-paper@4 \
  --set-input public-routes.wirescope=wirescope-beta.mc-remote.com

mcrctl deployment composition apply \
  --project <project> \
  --plan-id <generated-plan-id> \
  --yes
```

現行public betaでは、旧route adapter、WireScope同梱releaseへの前進、overlay canonicalizationを一つの
candidateへ投影する。旧homepage overlayが新Caddyfileを隠すため、通常updateとcanonicalizationを別々の
中間状態として実行しない。

`plan`はlive containerのCompose provenanceを自動取得し、既知の形だけを型へ変換する。周辺plugin JARは
filenameとSHA-256を持つ`minecraft-plugins@1`、homepage treeは決定論的inventory digestを持つ
`homepage-static@1`、writable backup bindはabsolute pathを持つ`minecraft-backup@1`となる。plugin bytesと
homepage treeは停止前にdeployment artifact storeへ取り込み、targetは追加Composeなしで起動する。

`/plugins`全体mount、別McRemote JAR、未知service／mount、生成済み内容と異なる外部`/config`、手書きの
homepage Caddyfileは採用せず停止する。recovery directoryに残ったstaleなMcRemote JARを「近くにあるから」
採用しない。target doctorは周辺plugin、homepage、backupのexact mountと`render=current`を検証する。
失敗時のrollbackはsnapshotしたsource overlayで旧projectionを再起動するが、world、session、pairingの
完全復元は主張しない。

## 8. runbookの更新規律

live作業で予期しない停止が起きたら、会話内の回避commandだけで先へ進まない。同じ作業中に次を行う。

1. 再現条件またはfixtureを追加
2. 機械検出できる問題をCLIのpreflight／doctorへ追加
3. 人間判断だけをrunbookへ追加
4. 修正版経路でresume

runbookは事故日記ではなく、現在の最短正準経路を先頭に置く。旧release救済手順はhistory節へ移し、
通常利用者がb2→b3→b4の全手順を読まないとb5を更新できない構造にしない。

## 9. 通常更新の合格条件

public betaの通常更新は次を運用SLOとする。

- host準備後の通常操作はplanとapplyの二command
- 人間checkpointはplan reviewと`--yes`の一回
- SHA、container ID、Compose pathの手転記ゼロ
- project fileのsudo編集、root owner修復、generated手編集ゼロ
- mutation後に新しいpreflight blockerを発見しない
- 失敗時もdurable plan／phaseから一commandでresume
- 正常時の人間操作時間15分以内を目標とし、server起動待ちは別計測

## 10. 実装順

1. operator bootstrap／check、root実行拒否（完了）
2. generic same-volume plan／apply、live Compose自動snapshot、doctor、限定rollback（第1slice完了）
3. public runbookを通常入口中心に再編集し、`public-bN`をhistory-only表記（完了）
4. public betaのrecovery plugin／homepage／backup canonicalization（実装完了、live適用待ち）
5. blocker集約、credential health、DNS／TLS claimをprofile policyで追加
6. home alpha／betaとcatering hostへ同じtransactionを投影

既存release固有migrationを新release用にコピーしてはならない。
