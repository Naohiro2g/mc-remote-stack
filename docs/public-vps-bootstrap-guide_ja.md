# 既存Public VPS same-volume更新runbook

このrunbookは、review済みのMcRemote release setで、同一world volumeを継承する既存deploymentを更新する
現行手順である。current canonical TOML stateを入力に、`deployment update plan`、
`deployment update apply`、`doctor`の三段階で完了する。対応済みpresetを使うcompact state adoption済みdeploymentは、
READMEのcompact `apply`／`doctor`を使う。public VPS向けcompact profileと既存VPSのcompact state adoptionは、
別の実装作業として扱う。

新しいUbuntu hostのoperator環境は、先に
[`fresh host bootstrap`](fresh-host-bootstrap-guide_ja.md)で準備する。このrunbookは、Stack担当がbackstage inventoryを読み、
対象host、Stack checkout、既存deployment projectを一組にした地点から始める。読み取りaccessが無い場合は、
Stack担当がhuman operatorへ申請する。

## 1. deployment handoffを受け取る

exact presetがまだ無いreleaseは、先に
[`release artifact／preset準備runbook`](release-preset-preparation-guide_ja.md)で公式配布物を照合し、
push済みのimmutable preset refを作る。preset準備後は、以下のdeployment handoffから上から順に実行する。

handoffには次の値が一組で入る。

| 値 | 内容 | 所有元 |
| --- | --- | --- |
| `MC_REMOTE_TARGET` | 対象hostのSSH接続先 | backstage inventoryをStackが取得 |
| `MC_REMOTE_KNOWLEDGE_COMMIT` | 今回参照するknowledge commit | Knowledge SSOT |
| `MC_REMOTE_STACK` | 対象host上のreview済みStack checkout | Stack |
| `MC_REMOTE_STACK_COMMIT` | checkoutのexact commit | Stack |
| `MC_REMOTE_PROJECT` | 対象host上のdeployment project | Stack／backstage |
| `MC_REMOTE_PROFILE` | 更新先のexact profile revision | Stackが依頼と現行stateから確定 |
| `MC_REMOTE_PRESET` | 更新先のexact preset revision | Stackがrelease handoffから確定 |
| `authorized next action` | 今回実行するpublic VPS update | human operator |

release済みsetではgate coordinatorを通常handoffの必須者にしない。candidate setをshared環境へ配置する場合だけ、
gate coordinatorがexact setとauthorized next actionを渡す。指定された`MC_REMOTE_KNOWLEDGE_COMMIT`では次を読む。

- [release operations responsibility](https://github.com/Naohiro2g/mc-remote-knowledge/blob/main/00-hub/release-operations-responsibility-design_ja.md):
  host写像、private情報、deploy／doctorの実行担当
- [release gate notes](https://github.com/Naohiro2g/mc-remote-knowledge/blob/main/00-hub/release-gate-notes_ja.md):
  candidate setを扱う場合のexact setとauthorized next action

operator向けの実行手順は、このrunbookを正本とする。

## 2. 更新前の人間checkpoint

typed operator input（Notice、接続先、plugin一覧等）を変更する更新では、作業前に人間が次を決める。
Stack担当はこれらを会話で確認せずに、既存project内の値や他projectの値をそのまま複製・推測して
進めない。

- Notice文言・リンク先が現在も正しいか（過去のreview用ファイルや他environmentのnoticeをそのまま
  採用しない）
- 引き継ぐworld内のplugin設定／DB（LuckPermsの権限設定等）を変更する必要が無いか
- maintenance開始、停止許容時間

稼働中projectの`mc-remote.toml`／`mc-remote.lock.toml`／`generated/`を、人間がその場で直接編集したり、
Stack担当が`resolve`／`render`を単独実行したりしない。理由は`8. 稼働中projectへのresolve／render単独
実行を避ける理由`を参照。typed inputの変更は、必ずproject外のreview用copyを編集し、
`--replace-input`で渡す（`4. exact update planを作る`参照）。

## 3. 対象hostで正準環境を確認する

管理端末からhandoffの接続先へ入る。

```sh
MC_REMOTE_TARGET="<handoffのSSH接続先>"
ssh "$MC_REMOTE_TARGET"
```

fresh host bootstrapを完了した新しいlogin sessionでhandoff値を設定する。
`uv`を含むoperator toolchainは、この時点ですべてcommand名だけで実行できる。

```sh
MC_REMOTE_STACK="<handoffのStack checkout>"
MC_REMOTE_STACK_COMMIT="<handoffのStack commit>"
MC_REMOTE_PROJECT="<handoffのdeployment project>"
MC_REMOTE_PROFILE="<handoffのexact profile>"
MC_REMOTE_PRESET="<handoffのexact preset>"

uv --version
test "$(git -C "$MC_REMOTE_STACK" rev-parse HEAD)" = "$MC_REMOTE_STACK_COMMIT"
uv sync --project "$MC_REMOTE_STACK" --frozen --extra dev
"$MC_REMOTE_STACK/tools/bootstrap-ubuntu-operator.sh" --check
uv run --project "$MC_REMOTE_STACK" mcrctl operator check \
  --project "$MC_REMOTE_PROJECT" \
  --docker-context default
test -f "$MC_REMOTE_PROJECT/mc-remote.toml"
```

ここまでの成功で、Python、Docker、Compose、operator権限、project owner、local Docker context、
Stack commitが一組に揃う。

## 4. exact update planを作る

```sh
uv run --project "$MC_REMOTE_STACK" \
  mcrctl deployment update plan \
  --project "$MC_REMOTE_PROJECT" \
  --docker-context default \
  --to-profile "$MC_REMOTE_PROFILE" \
  --to-preset "$MC_REMOTE_PRESET" \
  --allow-unverified
```

planは現在のdeploymentをdoctorで確認し、更新先presetを解決し、必要なartifactをdigestで取得し、
target renderとsame-volume更新内容を生成する。出力された`PLAN deployment-update id=sha256:...`を
`MC_REMOTE_PLAN_ID`へ設定する。

```sh
MC_REMOTE_PLAN_ID="sha256:<plan出力のid>"
```

## 5. planを適用する

```sh
uv run --project "$MC_REMOTE_STACK" \
  mcrctl deployment update apply \
  --project "$MC_REMOTE_PROJECT" \
  --plan-id "$MC_REMOTE_PLAN_ID" \
  --yes
```

transactionは同じvolume identityでtargetを起動し、起動後doctorまで実行する。target検証が完了すると
`OK deployment-update status=complete`を返す。target起動またはdoctorが失敗した場合はsource projectionを
復帰し、同じ`MC_REMOTE_PLAN_ID`で再開できる状態を返す。

## 6. live deploymentを確認する

```sh
uv run --project "$MC_REMOTE_STACK" mcrctl doctor \
  --project "$MC_REMOTE_PROJECT" \
  --output "$MC_REMOTE_PROJECT/generated" \
  --docker-context default
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
stack commit: <MC_REMOTE_STACK_COMMIT>
project: <MC_REMOTE_PROJECT>
profile / preset: <MC_REMOTE_PROFILE> / <MC_REMOTE_PRESET>
plan id: <MC_REMOTE_PLAN_ID>
transaction: complete
doctor: <OK行>
next action: service継続
```

失敗時は同じ欄へtransaction phase、reason、source復帰結果、再開用plan IDを記録する。Stack担当は
その一組を入力に修復し、同じplanを再実行する。

## 8. 稼働中projectへのresolve／render単独実行を避ける理由

`mcrctl resolve`と`mcrctl render`を、稼働中containerを持つprojectへ直接（`deployment update
plan`／`apply`のtransactionを経由せず）実行すると、実機で次の破損が再現した。

- `render`はprojectの`generated/`を新しいinodeへ差し替える。ファイル単位でbind mountしている
  content（例: Scratch runtime configの`runtime/scratch.json`）は稼働中containerが古いinodeを
  保持し続けるため、見かけ上は壊れない。
- 一方、ディレクトリ単位でbind mountしているcontent（例: WireScopeの`generated/wirescope`
  → `/srv/wirescope`）は、稼働中containerのbind先が空になり、対象containerを再起動するまで
  404を返し続ける。

`deployment update plan`は候補を`.mcrctl/updates/`配下の隔離領域に作るため、この破損を起こさない。
`apply`はtarget起動を含む一つのtransactionとしてcontainerを作り直すため、bind mountも正しく
更新される（`5. planを適用する`）。稼働中projectを直接触る必要が生じた場合（`stale_lock`等）は、
このrunbookの通常経路（`--replace-input`）へ戻すか、Stack担当が対象containerを手動で再起動して
復旧してから、人間へ状況を報告する。
