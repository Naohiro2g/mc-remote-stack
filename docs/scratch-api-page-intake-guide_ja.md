# Scratch APIページをホームページ正本へ取り込む

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

取り込み結果とほかのコンテンツがknowledgeの`main`へ反映されたら、[ホームページの配信手順](homepage-sync-guide_ja.md)へ進みます。
