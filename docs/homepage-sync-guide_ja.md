# ホームページをデプロイする

公開VPSが配信するホームページの正本は、[mc-remote-knowledge](https://github.com/Naohiro2g/mc-remote-knowledge)の`30-広告宣伝/homepage/`です。「ホームページを更新して」は[Scratch APIページの確認・取り込み](scratch-api-page-intake-guide_ja.md)も含みます。コンテンツをknowledgeの`main`へ反映した後の「デプロイして」は、以下の配信手順です。

## 1. 配信対象を確認する

公式の対象hostとdeployment projectは、非公開の運用記録にある現行公開環境と今回の引継ぎで確かめます。過去のdeployment名から推測しません。対象hostでprojectのdirectoryに入ります。

```sh
cd "$HOME/mc-remote-deployments/<deployment名>"
mcrctl operator check
```

`mcrctl`が見つからない場合は、[PATHが通っていないとき](fresh-host-bootstrap-guide_ja.md#pathが通っていないとき)で戻します。

「ホームページを更新して」という全体更新では、公開中の`https://<Scratchのhostname>/mc-remote-product-config.json`にある**開発元のお知らせ**と、対象projectの`operator/connection-targets/targets.toml`にある**前回の運用者のお知らせ**を、見出し・本文・リンク先とともに提示し、更新の要否を決めます。確認した公開URLと、実ファイルがあればクリックできるファイルリンクも添えます。「お知らせ更新なし」が既に決まっている場合は再確認しません。「デプロイして」だけの依頼では、この確認を省略します。

お知らせを変える場合や**お知らせだけを更新する場合**は[Scratchのお知らせ編集手順](scratch-notice-guide_ja.md)に従います。`homepage sync`はお知らせを変更しません。

## 2. 配信する

knowledgeの`main`へ今回のコンテンツが反映されたら実行します。

```sh
mcrctl homepage sync
mcrctl doctor
```

`homepage sync`はknowledgeの`main`を一時的にshallow cloneし、配信用directoryを丸ごと入れ替えてからCaddyだけを作り直します。Minecraft、Scratch、Bridgeは止まりません。deployment lockは変わりません。`homepage sync`がFAILなら同じコマンドを一度だけ再実行します。再度FAILなら、出力をそのまま報告して止めます。

## 3. 公開結果を確認する

`doctor`が`OK doctor homepage=current`を表示し、今回変更した公開URLに反映されたら配信完了です。`doctor`がFAILなら出力を報告して止めます。Scratch APIページを取り込んだ場合は、`/api/scratch/`の一覧データ、JS・CSS・画像も確認します。結果には反映したknowledge commitと公開確認を残します。Scratchページを取り込んだ場合は取得commit・対象release・差分の有無も残します。非公開の実施記録はprivate opsへ残します。

配信内容を戻す場合は、knowledge側でrevertしてから、もう一度`homepage sync`を実行します。
