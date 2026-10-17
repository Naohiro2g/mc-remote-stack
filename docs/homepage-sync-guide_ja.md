# ホームページをデプロイする

公開VPSが配信するホームページの正本は、[mc-remote-knowledge](https://github.com/Naohiro2g/mc-remote-knowledge)の`30-広告宣伝/homepage/`です。コンテンツをknowledgeの`main`へ反映した後の「デプロイして」は、以下の配信手順です。「ホームページを更新して」という依頼でScratch APIページの確認・取り込みも必要な場合は、先に[Scratch APIページの取り込み手順](scratch-api-page-intake-guide_ja.md)を使います。

## 1. 対象とScratchのお知らせを確認する

公式の対象hostとdeployment projectは、`mc-remote-backstage`の`server-environment-inventory_ja.md`にあるcurrent public VPSの記録と今回の引継ぎで確かめます。過去のdeployment名から推測しません。対象hostでprojectのdirectoryに入ります。

```sh
cd "$HOME/mc-remote-deployments/<deployment名>"
mcrctl operator check
```

`mcrctl`が見つからない場合は、[PATHが通っていないとき](fresh-host-bootstrap-guide_ja.md#pathが通っていないとき)で戻します。

配信前に、公開中の`https://<Scratchのhostname>/mc-remote-product-config.json`にある**開発元のお知らせ**と、対象projectの`operator/connection-targets/targets.toml`にある**前回の運用者のお知らせ**を、見出し・本文・リンク先とともに依頼者へ提示します。確認した公開URLと、ローカルクローンやprojectに実ファイルがあればクリックできるファイルリンクも添えます。「変更なし」も選択肢です。回答を受けるまで変更を推測しません。運用者のお知らせを変える場合は[Scratchのお知らせ編集手順](scratch-notice-guide_ja.md)に従います。開発元のお知らせはScratchの配布イメージが所有します。`homepage sync`はどちらも変更しません。

## 2. 配信する

knowledgeの`main`へ今回のコンテンツが反映され、お知らせの扱いが決まったら実行します。

```sh
mcrctl homepage sync
mcrctl doctor
```

`homepage sync`はknowledgeの`main`を一時的にshallow cloneし、配信用directoryを丸ごと入れ替えてからCaddyだけを作り直します。Minecraft、Scratch、Bridgeは止まりません。deployment lockは変わりません。失敗時は配信用directoryが既に替わっている可能性があるため、状況を確認して報告します。

## 3. 公開結果を確認する

`doctor`の成功に加え、今回変更した公開URLを開いて反映結果を確認します。Scratch APIページを取り込んだ場合は、`/api/scratch/`の一覧データ、JS・CSS・画像も確認します。結果には反映したknowledge commit、公開確認、お知らせの扱いを残します。Scratchページを取り込んだ場合は取得commit・対象release・差分の有無も残します。privateな実施記録はprivate opsへ残します。

配信内容を戻す場合は、knowledge側でrevertしてから、もう一度`homepage sync`を実行します。
