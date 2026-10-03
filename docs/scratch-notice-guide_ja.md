# Scratchのお知らせを編集する

releaseのpresetを決めた後、配備するお知らせを一時ファイルで編集し、設定入力へ収容する手順です。人間もエージェントも同じファイルを編集できます。適用後は、配信される設定とScratchのお知らせペインを確認します。

[VPS更新手順](public-vps-bootstrap-guide_ja.md)ではrequest確認からplan作成まで、[別セットの構築準備](public-vps-preparation-guide_ja.md)では次セットの入力を作る段階で行います。最後の表示確認は、そのセットを起動した後に行います。

## 1. 表示されるお知らせと編集対象を確認する

現行の[deployment interface設計 §3](https://github.com/Naohiro2g/mc-remote-knowledge/blob/main/00-hub/deployment-interface-design_ja.md#3-設定は所有者ごとに二つへ分ける)では、Scratchは次の二つを別々に読みます。

| お知らせ | 元のファイル | 今回の編集対象 |
| --- | --- | --- |
| 運用者からのお知らせ | Stackが入力から生成する`mc-remote-runtime-config.json` | 元のoperator入力またはホーム用order |
| 開発元・製品からのお知らせ | Scratchイメージ内の`mc-remote-product-config.json` | 配布イメージに含まれる内容を確認 |

表示順は運用者notice、製品noticeです。運用者noticeを変更しても製品noticeは残ります。[開発元noticeの既存決定（2026-09-05-02）](https://github.com/Naohiro2g/mc-remote-knowledge/blob/main/00-hub/DECISIONS_ja.md)も、表示する版と実際のbuildのずれ、同じ告知の重複表示を解消するため、開発元noticeをScratchのproduct-configへ一本化しています。

製品noticeはScratchのreleaseで提供され、配備するイメージから内容を確認できます。サーバーごとの案内や追加トピックは運用者noticeに収容します。以下では、その運用者noticeを一時ファイルで編集・追加・削除してから配備します。

対象host上で、operator環境を確認済みのdeployment projectへ入ります。別セットを準備する場合は、まだ稼働していない次セットのprojectを使います。

```sh
cd "<今回編集するdeployment project>"
NOTICE_REVIEW="$(mktemp -d "${TMPDIR:-/tmp}/mc-remote-notice.XXXXXX")"
```

対象releaseの製品noticeは、release収集で照合したScratchイメージから確認できます。稼働中の旧releaseのイメージと取り違えないよう、対象presetの`scratch-runtime`に対応するartifactの`locator@digest`を使います。

```sh
NOTICE_SCRATCH_IMAGE="<照合済みScratchイメージのlocator>@sha256:<digest>"
docker pull "$NOTICE_SCRATCH_IMAGE"
docker run --rm --network none --entrypoint cat "$NOTICE_SCRATCH_IMAGE" \
  /usr/share/nginx/html/mc-remote-product-config.json \
  > "$NOTICE_REVIEW/product-notices.json"
cat "$NOTICE_REVIEW/product-notices.json"
```

この一時containerはファイルを読み取って終了します。配備先のサービスやportは起動しません。取得に失敗した場合は、その失敗を「製品noticeなし」と区別して確認します。

既存の運用者noticeと今回の製品noticeを提示し、運用者noticeを継承／編集／追加／削除する項目を決めます。会話で既に決まっている内容を使い、未確認の文面・リンク先・適用先を補います。以前のreview用ファイルや別環境の運用者noticeを使う場合も、今回の対象に合う内容かを確認します。

## 2. VPS用の一時ファイルを編集する

現行VPSの`connection-targets@3`では、接続先と運用者noticeが同じ入力ファイルに入ります。以下は標準pathです。orderの`operator_inputs`でroleが`connection-targets`のpathを確認し、異なる場合は読み替えます。

```sh
mcrctl operator check
NOTICE_INPUT="operator/connection-targets/targets.toml"
cp "$NOTICE_INPUT" "$NOTICE_REVIEW/targets.before.toml"
cp "$NOTICE_INPUT" "$NOTICE_REVIEW/targets.toml"
```

人間またはエージェントが`$NOTICE_REVIEW/targets.toml`を編集します。`[[targets]]`は接続先なので、noticeだけを変更する作業ではその内容を維持します。`[[notices]]`一組が一つのトピックです。

```toml
# [[targets]] は元のファイルの内容を維持する

[[notices]]
heading = "このサーバーの利用について"
body = "本日の利用時間は16時までです。"

[[notices]]
heading = "今回のリリース"
body = "変更点と使い方はこちらで確認できます。"
href = "https://example.org/releases/"
label = "変更点を見る"
```

- 編集: 対象の`heading`、`body`、必要ならリンクを変更する。
- 追加: `[[notices]]`をもう一組追加する。ファイルに並べた順で表示される。
- 削除: 対象の`[[notices]]`から次のtable直前までを削除する。
- リンクを外す: `href`と`label`を両方削除する。リンクが無いnoticeも使える。

現行VPS adapterはnoticeを1〜16件受け付けます。全件削除は入力検査で失敗します。見出しとリンク文字列は64文字まで、本文は512文字までの空でない一行のテキストです。HTMLとしては表示されません。リンクは絶対HTTPS URLで、`href`と`label`を組にします。改行・制御文字、前後の空白、URL内の認証情報やfragmentは入力検査で失敗します。

```sh
diff -u "$NOTICE_REVIEW/targets.before.toml" "$NOTICE_REVIEW/targets.toml"
```

`diff`の終了値1は差分があるという意味です。対象・文面・リンク先・並び順を確認し、接続先が意図せず変わっていないことも確認します。noticeは利用者へ配信される内容です。

## 3. VPSの設定入力へ収容する

### 稼働中の同じセットを更新する場合

稼働中projectへの直接コピーはせず、review済みのファイルを更新planへ渡します。noticeだけを更新する場合は、現在のexact profile／presetを指定します。release更新と一緒に行う場合は、その更新先を指定します。

```sh
mcrctl deployment update plan \
  --to-profile "<今回のexact profile>" \
  --to-preset "<今回のexact preset>" \
  --replace-input "connection-targets=$NOTICE_REVIEW/targets.toml"
```

planは隔離した候補projectへコピーし、入力検査とrenderを行います。plan成功後は、返されたIDに対応する候補の`operator/connection-targets/targets.toml`と`generated/runtime/scratch.json`を確認できます。

```sh
NOTICE_PLAN_ID="sha256:<planが返したID>"
NOTICE_CANDIDATE=".mcrctl/updates/${NOTICE_PLAN_ID#sha256:}/candidate"
cat "$NOTICE_CANDIDATE/generated/runtime/scratch.json"
```

候補が決まった後で一時ファイルを再編集しても、作成済みplanの内容は変わりません。変更した場合は新しいplanを作り、そのIDを使います。適用は[VPS更新手順のplan適用](public-vps-bootstrap-guide_ja.md#5-planを適用する)へ進みます。noticeだけの変更でも、この経路はcontainerを作り直すので、停止時間を見込んで実行します。

### まだ稼働していない別セットを作る場合

次セットのprojectでreview済みの入力を収容します。

```sh
cp "$NOTICE_REVIEW/targets.toml" "$NOTICE_INPUT"
```

[構築準備の設定・配布物確認](public-vps-preparation-guide_ja.md#4-設定と配布物を確認する)へ進みます。生成後の`generated/runtime/scratch.json`を読み、入力したnoticeと一致することを確認します。起動前の確認では、配信と画面表示はまだ確認できません。

## 4. ホーム用のコンパクトorderを使う場合

`mcrctl apply ./mc-remote.toml`を使う[ホーム用手順](home-catering-guide_ja.md)では、運用者noticeをorder内へ書きます。VPSの`--replace-input`は使いません。orderを一時directoryへコピーし、そのコピーを編集します。

```sh
cp ./mc-remote.toml "$NOTICE_REVIEW/mc-remote.toml"
```

現行のホーム用orderは0件または1件です。既存の1件を編集・削除するか、無い場合に1件を追加できます。リンクの形式もVPS入力とは異なります。

```toml
[[notices]]
heading = "このホームセットについて"
body = "本日はこのセットで試してください。"
link = { href = "https://example.org/guide/", label = "利用案内" }
```

```sh
diff -u ./mc-remote.toml "$NOTICE_REVIEW/mc-remote.toml"
mcrctl apply "$NOTICE_REVIEW/mc-remote.toml" --dry-run \
  --output "$NOTICE_REVIEW/preview"
cat "$NOTICE_REVIEW/preview/runtime/scratch.json"
```

内容を確認したコピーを元のorderへ収容し、ホーム用手順のapply／doctorへ進みます。同じdeploymentへのapplyも起動・更新を行う操作です。複数のトピック追加は、このorder形式の現行実装では受け付けません。

## 5. 適用後に配信とお知らせペインを確認する

対応するrunbookでapplyとdoctorを終えたら、今回配備したScratchのURLを開き直します。ホームの旧releaseや別セットのURLと取り違えないよう、表示された接続先も確認します。

1. お知らせペインを開き、編集後の見出し・本文・順序を確認する。
2. 削除した運用者トピックが消え、追加したトピックが表示されることを確認する。
3. リンクの文字列と、開く先が今回指定したものかを確認する。
4. 後ろに表示される製品noticeを確認し、運用者noticeとの重複を確認する。

doctorは配信runtimeの一致とschemaを検査します。製品noticeの文面やブラウザー上の表示・リンク操作まで確認した結果にはなりません。

表示が違うときは、入力→候補の生成物→配信される設定→画面の順で照合します。配信の確認には、今回のScratch URLを使います。

```sh
NOTICE_SCRATCH_URL="https://<今回のScratch hostname>/"
curl -fsS "${NOTICE_SCRATCH_URL%/}/mc-remote-runtime-config.json" \
  > "$NOTICE_REVIEW/served-runtime.json"
curl -fsS "${NOTICE_SCRATCH_URL%/}/mc-remote-product-config.json" \
  > "$NOTICE_REVIEW/served-product.json"
```

運用者側から削除した文面が製品側にも含まれていれば、画面に残る理由を区別できます。生成物と配信結果が違う場合は、対象URL、current lock、mount、更新結果を確認します。稼働中の生成JSONを編集しても元の入力には戻らず、mountの差し替えだけではcontainerが以前のファイルを保持する場合があります（[VPS更新手順の説明](public-vps-bootstrap-guide_ja.md#8-稼働中projectへのresolverender単独実行を避ける理由)）。

最終的な文面と差分、対象project／release、plan IDまたはapply結果、doctor結果、画面で確認した項目をprivate opsへ残します。一時ファイルは編集場所であり、継続して使う入力は収容先のファイルです。会話が流れても、採用した内容と未確認の表示項目を次の担当が追えるようにします。
