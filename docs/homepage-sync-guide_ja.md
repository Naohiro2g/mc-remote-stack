# ホームページを更新する

公開VPSが配信するホームページの正本は、[mc-remote-knowledge](https://github.com/Naohiro2g/mc-remote-knowledge)の`30-広告宣伝/homepage/`です。「ホームページを更新して」という依頼では、ScratchのAPIページも確認し、必要な取り込みを済ませてから配信を更新します。

## 1. ScratchのAPIページを確認して正本へ取り込む

生成済みページの所有者は[scratch-editor](https://github.com/Naohiro2g/scratch-editor)です。更新担当は、次の入力と出力を確認します。

| 対象 | path |
| --- | --- |
| Scratch側の生成済みページ | `mc-remote/block-reference/site/` |
| ホームページ正本への取り込み先 | knowledgeの`30-広告宣伝/homepage/api/scratch/` |
| 公開URL | `https://mc-remote.com/api/scratch/` |

1. Scratchの生成済みページを取得し、取得元のcommitを記録します。取得には対象commitのGitHub archiveまたは使い捨てのread-only cloneを使います。利用者の作業中checkoutは変更しません。
2. `blocks.json`の`release`、`sourceRef`、`sourceCommit`を確認し、公開済みreleaseのtag targetと一致することをGitHub APIまたはGitで確かめます。ページを取得したcommitと、ページが説明するreleaseの`sourceCommit`は区別して記録します。
3. `index.html`、`blocks.json`、`page.js`、`style.css`、`images/`を含むdirectory全体を、knowledge側のホームページ正本と比較します。差分があればknowledge側の作業として`api/scratch/`を置き換え、追加・削除も反映します。ほかのホームページ内容と合わせて`main`へ反映し、リモートへの反映を確認します。差分がなければ、同じ内容であることを記録して次へ進みます。
4. 取り込み先のfile集合と各fileのbytes／SHA-256が生成元に一致し、ページが参照する画像・JS・CSSが揃っていることを確かめます。

この作業では生成済みのページを取り込みます。ブロック画像の再生成は[Scratch側の生成手順](https://github.com/Naohiro2g/scratch-editor/blob/develop/mc-remote/block-reference/README.md)に従う別作業です。必要な公開版の生成物がない場合や、metadataが公開releaseと一致しない場合は、その不足を更新結果へ記載します。candidateのページを公開版として取り込みません。

取り込みのためのknowledge側の編集は、knowledge担当が所有する作業環境で行います。SSOT参照に使ったread-only cloneを編集・push用へ転用しません。

## 2. hostを確認して配信を更新する

コマンドは対象hostで、deployment projectのdirectoryに入って実行します。最初に環境を確かめます。

```sh
cd "$HOME/mc-remote-deployments/<deployment名>"
mcrctl operator check
```

`mcrctl`が見つからない場合は、[PATHが通っていないとき](fresh-host-bootstrap-guide_ja.md#pathが通っていないとき)で戻します。

Scratchページの取り込みを含む変更がknowledgeの`main`へ反映されたら、次を実行します。

```sh
mcrctl homepage sync
mcrctl doctor
```

`homepage sync`は、knowledgeの`main`を一時的にshallow cloneし、配信用directoryを丸ごと入れ替えてから、Caddyだけを作り直します。Minecraft、Scratch、Bridgeは止まりません。deployment lockは変わりません。

## 3. 公開結果を確認する

`doctor`の成功に加えて、公開HTTPSの`/api/scratch/`、一覧データ、JS・CSS・画像が取得でき、反映したknowledgeの正本と一致することを確認します。更新結果には、Scratchページの取得commitと対象release、反映したknowledge commit、取り込み差分の有無、公開確認の結果を残します。privateなhost・deployment・実施記録はprivate opsへ残します。

配信内容を戻したいときは、knowledge側でrevertしてから、もう一度`homepage sync`を実行します。
