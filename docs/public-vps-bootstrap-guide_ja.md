# Public VPS release deployment runbook

このrunbookは、review済みのMcRemote release setを、既存のcanonical TOML deploymentへ
same-volume更新する正準手順である。作業者は上から順に実行し、`deployment update plan`、
`deployment update apply`、`doctor`の三段階で完了する。

新しいUbuntu hostのoperator環境は、先に
[`fresh host bootstrap`](fresh-host-bootstrap-guide_ja.md)で準備する。このrunbookは、対象hostへの
SSH接続、Stack checkout、既存deployment projectがhandoff済みの地点から始める。

## 1. deployment handoffを受け取る

handoffには次の値が一組で入る。

| 値 | 内容 | 所有元 |
| --- | --- | --- |
| `MC_REMOTE_TARGET` | 対象hostのSSH接続先 | backstage |
| `MC_REMOTE_KNOWLEDGE_COMMIT` | 今回参照するknowledge commit | gate coordinator |
| `MC_REMOTE_STACK` | 対象host上のreview済みStack checkout | Stack |
| `MC_REMOTE_STACK_COMMIT` | checkoutのexact commit | Stack |
| `MC_REMOTE_PROJECT` | 対象host上のdeployment project | Stack／backstage |
| `MC_REMOTE_PROFILE` | 更新先のexact profile revision | gate coordinator |
| `MC_REMOTE_PRESET` | 更新先のexact preset revision | gate coordinator |
| `authorized next action` | 今回実行するpublic VPS update | gate coordinator／release owner |

handoffの根拠は、指定された`MC_REMOTE_KNOWLEDGE_COMMIT`で次の二文書へ接続する。

- [release gate notes](https://github.com/Naohiro2g/mc-remote-knowledge/blob/main/00-hub/release-gate-notes_ja.md):
  exact setとauthorized next action
- [release operations responsibility](https://github.com/Naohiro2g/mc-remote-knowledge/blob/main/00-hub/release-operations-responsibility-design_ja.md):
  host写像、private情報、deploy／doctorの実行担当

operator向けの実行手順は、このrunbookを正本とする。

## 2. 対象hostで正準環境を確認する

管理端末からhandoffの接続先へ入る。

```sh
MC_REMOTE_TARGET="<handoffのSSH接続先>"
ssh "$MC_REMOTE_TARGET"
```

対象host上でhandoff値を設定する。`uv`の正準install先は
`$HOME/.local/bin/uv`である。

```sh
MC_REMOTE_UV="$HOME/.local/bin/uv"
MC_REMOTE_STACK="<handoffのStack checkout>"
MC_REMOTE_STACK_COMMIT="<handoffのStack commit>"
MC_REMOTE_PROJECT="<handoffのdeployment project>"
MC_REMOTE_PROFILE="<handoffのexact profile>"
MC_REMOTE_PRESET="<handoffのexact preset>"

test -x "$MC_REMOTE_UV"
test "$(git -C "$MC_REMOTE_STACK" rev-parse HEAD)" = "$MC_REMOTE_STACK_COMMIT"
"$MC_REMOTE_UV" sync --project "$MC_REMOTE_STACK" --frozen --extra dev
"$MC_REMOTE_STACK/tools/bootstrap-ubuntu-operator.sh" --check
"$MC_REMOTE_UV" run --project "$MC_REMOTE_STACK" mcrctl operator check \
  --project "$MC_REMOTE_PROJECT" \
  --docker-context default
test -f "$MC_REMOTE_PROJECT/mc-remote.toml"
```

ここまでの成功で、Python、Docker、Compose、operator権限、project owner、local Docker context、
Stack commitが一組に揃う。

## 3. exact update planを作る

```sh
"$MC_REMOTE_UV" run --project "$MC_REMOTE_STACK" \
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

## 4. planを適用する

```sh
"$MC_REMOTE_UV" run --project "$MC_REMOTE_STACK" \
  mcrctl deployment update apply \
  --project "$MC_REMOTE_PROJECT" \
  --plan-id "$MC_REMOTE_PLAN_ID" \
  --yes
```

transactionは同じvolume identityでtargetを起動し、起動後doctorまで実行する。target検証が完了すると
`OK deployment-update status=complete`を返す。target起動またはdoctorが失敗した場合はsource projectionを
復帰し、同じ`MC_REMOTE_PLAN_ID`で再開できる状態を返す。

## 5. live deploymentを確認する

```sh
"$MC_REMOTE_UV" run --project "$MC_REMOTE_STACK" mcrctl doctor \
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

## 6. handoffを完了する

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
