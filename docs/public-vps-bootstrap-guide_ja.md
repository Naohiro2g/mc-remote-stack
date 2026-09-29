# 既存Public VPS same-volume更新runbook

このrunbookは、review済みのMcRemote release setで、同一world volumeを継承する既存deploymentを更新する現行手順である。current canonical TOML stateを入力に、`deployment update plan`、`deployment update apply`、`doctor`の三段階で完了する。

新しいUbuntu hostのoperator環境は、先に[`fresh host bootstrap`](fresh-host-bootstrap-guide_ja.md)で準備する。このrunbookは、Stack担当がbackstage inventoryを読み、対象host、Stack checkout、既存deployment projectを一組にした地点から始める。読み取りaccessが無い場合は、Stack担当がhuman operatorへ申請する。

## 1. deployment handoffを受け取る

exact presetがまだ無いreleaseは、先に[`release artifact／preset準備runbook`](release-preset-preparation-guide_ja.md)で公式配布物を照合し、push済みのimmutable preset refを作る。preset準備後は、以下のdeployment handoffから上から順に実行する。

handoffには次の値が一組で入る。

| 値 | 内容 | 所有元 |
| --- | --- | --- |
| SSH接続先 | 対象hostのSSH接続先 | backstage inventoryをStackが取得 |
| knowledge commit | 今回参照するknowledge commit | Knowledge SSOT |
| Stack commit | 対象hostのStack checkout（`~/mc-remote-stack`）のexact commit | Stack |
| deployment project | 対象host上のdeployment projectのpath | Stack／backstage |
| exact profile | 更新先のprofile revision | Stackが依頼と現行stateから確定 |
| exact preset | 更新先のpreset revision | Stackがrelease handoffから確定 |
| `authorized next action` | 今回実行するpublic VPS update | human operator |

release済みsetではgate coordinatorを通常handoffの必須者にしない。candidate setをshared環境へ配置する場合だけ、gate coordinatorがexact setとauthorized next actionを渡す。指定されたknowledge commitでは次を読む。

- [release operations responsibility](https://github.com/Naohiro2g/mc-remote-knowledge/blob/main/00-hub/release-operations-responsibility-design_ja.md): host写像、private情報、deploy／doctorの実行担当
- [release gate notes](https://github.com/Naohiro2g/mc-remote-knowledge/blob/main/00-hub/release-gate-notes_ja.md): candidate setを扱う場合のexact setとauthorized next action

operator向けの実行手順は、このrunbookを正本とする。

## 2. 収集直後のrequest確認

preset収集（`release artifact／preset準備runbook`）が終わった直後、typed operator inputへ進む前に、人間へ次を提示しrequestを聞く。Stack担当はこれらを会話で確認せずに、既存project内の値や他projectの値をそのまま複製・推測して進めない。

- preset: 収集で確定したexact preset refをそのまま採用するか、追加のrequestがあるか
- notice: 対象releaseの製品側notice（あれば）と、既存projectの運用者notice（文言・リンク先を含む）を提示し、継承／編集／追加／削除のいずれかを選ばせる（過去のreview用ファイルや他environmentのnoticeをそのまま採用しない）
- 引き継ぐworld内のplugin設定／DB（LuckPermsの権限設定等）を変更する必要が無いか
- maintenance開始、停止許容時間

稼働中projectの`mc-remote.toml`／`mc-remote.lock.toml`／`generated/`を、人間がその場で直接編集したり、Stack担当が`resolve`／`render`を単独実行したりしない。理由は`8. 稼働中projectへのresolve／render単独実行を避ける理由`を参照。typed inputの変更は、必ずproject外のreview用copyを編集し、`--replace-input`で渡す（`4. exact update planを作る`参照）。

## 3. 対象hostで正準環境を確認する

管理端末からhandoffの接続先へ入る。

```sh
ssh "<handoffのSSH接続先>"
```

Stack checkoutがhandoffのcommitであることを確かめ、環境を揃える。

```sh
cd ~/mc-remote-stack
test "$(git rev-parse HEAD)" = "<handoffのStack commit>"
uv sync --locked
tools/bootstrap-ubuntu-operator.sh --check
```

以降のcommandはdeployment projectのdirectoryで実行する。

```sh
cd "<handoffのdeployment project>"
mcrctl operator check
```

ここまでの成功で、Python、Docker、Compose、operator権限、project owner、local Docker context、Stack commitが一組に揃う。`mcrctl`が見つからない場合は、[PATHが通っていないとき](fresh-host-bootstrap-guide_ja.md#pathが通っていないとき)で戻す。

## 4. exact update planを作る

```sh
mcrctl deployment update plan \
  --to-profile "<handoffのexact profile>" \
  --to-preset "<handoffのexact preset>"
```

planは現在のdeploymentをdoctorで確認し、更新先presetを解決し、必要なartifactをdigestで取得し、target renderとsame-volume更新内容を生成する。出力の`PLAN deployment-update id=sha256:...`が次の手順で使うplan IDである。

## 5. planを適用する

```sh
mcrctl deployment update apply --plan-id "sha256:<plan ID>" --yes
```

transactionは同じvolume identityでtargetを起動し、起動後doctorまで実行する。target検証が完了すると`OK deployment-update status=complete`を返す。target起動またはdoctorが失敗した場合はsource projectionを復帰し、同じplan IDで再開できる状態を返す。

## 6. live deploymentを確認する

```sh
mcrctl doctor
```

完了時は次の状態が一度に確認できる。

- runtimeがhealthy
- renderがcurrent
- exact lockと稼働artifactが一致
- public portとnetwork projectionが一致
- Scratch runtime configがcurrent
- Bridge allowlistとScratch target集合が一致
- McRemote protocolがresponsiveで認証を要求
- homepageとWireScopeの配信内容がcurrent

## 7. handoffを完了する

作業結果として次の値を返す。

```text
target: <backstage上の参照>
stack commit: <Stack commit>
project: <deployment project>
profile / preset: <exact profile> / <exact preset>
plan id: <plan ID>
transaction: complete
doctor: <OK行>
next action: service継続
```

失敗時は同じ欄へtransaction phase、reason、source復帰結果、再開用plan IDを記録する。Stack担当はその一組を入力に修復し、同じplanを再実行する。

## 8. 稼働中projectへのresolve／render単独実行を避ける理由

`mcrctl resolve`と`mcrctl render`を、稼働中containerを持つprojectへ直接（`deployment update plan`／`apply`のtransactionを経由せず）実行すると、実機で次の破損が再現した。

- `render`はprojectの`generated/`を新しいinodeへ差し替える。ファイル単位でbind mountしているcontent（例: Scratch runtime configの`runtime/scratch.json`）は稼働中containerが古いinodeを保持し続けるため、見かけ上は壊れない。
- 一方、ディレクトリ単位でbind mountしているcontent（例: WireScopeの`generated/wirescope`→ `/srv/wirescope`）は、稼働中containerのbind先が空になり、対象containerを再起動するまで404を返し続ける。

`deployment update plan`は候補を`.mcrctl/updates/`配下の隔離領域に作るため、この破損を起こさない。`apply`はtarget起動を含む一つのtransactionとしてcontainerを作り直すため、bind mountも正しく更新される（`5. planを適用する`）。稼働中projectを直接触る必要が生じた場合（`stale_lock`等）は、このrunbookの通常経路（`--replace-input`）へ戻すか、Stack担当が対象containerを手動で再起動して復旧してから、人間へ状況を報告する。
