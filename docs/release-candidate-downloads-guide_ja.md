# リリース候補を配る

凍結したリリース候補を、公開VPSの`https://<ホームページのhostname>/candidates/<版>/<set>/`に置いて、試験する人がブラウザで取れるようにする手順です。リリースの後に消します。

この経路は、profile `vps-server@13`以降の公開VPSの設定にあります。Caddyは`/candidates/`の下を、ホームページとは別のディレクトリから読み取り専用で配ります。`X-Robots-Tag: noindex, nofollow`を付け、再圧縮せず、Rangeに応え、ディレクトリの一覧を出しません。`.zip`と`.whl`は`application/zip`、`.jar`は`application/java-archive`で返します。どこからもリンクしません。

## 1. 置き場所を確認する

対象hostでdeployment projectのdirectoryに入ります。

```sh
cd "$HOME/mc-remote-deployments/<deployment名>"
mcrctl operator check
grep '^artifact_store' mc-remote.toml
```

`mcrctl`が見つからない場合は、[PATHが通っていないとき](fresh-host-bootstrap-guide_ja.md#pathが通っていないとき)で戻します。

置き場所は、表示された`artifact_store`の一つ上のディレクトリにある`candidates`です（ホームページの`homepage`と同じ場所）。以下では、このpathを`CANDIDATES`とします。

```sh
CANDIDATES="<artifact_storeの一つ上のディレクトリ>/candidates"
```

## 2. 経路を持たない公開VPSを移す（一度だけ）

稼働中のdeploymentが`vps-server@12`の場合は、先に置き場所を作り、profileだけを`vps-server@13`へ進めます。presetは今のままです。

```sh
install -d -m 755 "$CANDIDATES"
mcrctl deployment update plan --to-profile vps-server@13 --to-preset "<今のpreset>"
mcrctl deployment update apply --plan-id "sha256:<plan ID>" --yes
mcrctl doctor
```

この更新は[公開VPSの更新](public-vps-bootstrap-guide_ja.md)と同じtransactionで、Caddy、Scratch、Bridge、Minecraftをすべて止めてから起動し直します。実施の時期はhuman ownerが決めます。`doctor`で、ホームページとScratchが元どおりであることを確かめます。

`vps-server@13`のrenderは、`candidates`ディレクトリが無いと`runtime_content_missing`で止まります。新しいセットを`vps-server@13`で作るときも、同じディレクトリを先に作ります。

## 3. 候補を置く

置くファイルとbytes／SHA-256は、knowledgeの凍結の表（`00-hub/release-gate-notes_ja.md`）に従います。手元でGitHubのartifactから取り出し、凍結値の全桁と照合してから送ります。ファイル名はReleaseのassetと同じにします。

手元で、照合済みのファイルを一つのdirectoryに集め、凍結値から`SHA256SUMS`を作ります。

```sh
cd "<照合済みファイルのdirectory>"
sha256sum -c SHA256SUMS
rsync --partial --mkpath --chmod=D755,F644 -av ./ "<SSH接続先>:<CANDIDATESのpath>/<版>/<set>/"
```

対象hostで照合します。

```sh
cd "$CANDIDATES/<版>/<set>"
sha256sum -c SHA256SUMS
```

すべて`OK`なら配置は完了です。置いたURLと`sha256sum -c`の結果を返します。外からの照合は、配布を頼んだ側が行います。`rsync`が途中で切れたら、同じコマンドをもう一度実行すると続きから送ります。照合が`FAILED`なら、そのファイルを消し、出力をそのまま報告して止めます。

試験で候補を作り直した場合は、新しい`<set>`（例：`set-2`）に置きます。同じURLの中身は差し替えません。

## 4. リリースの後に消す

リリースの後、Releaseのassetが凍結値と同じbytesであることの照合が済んだら、版のdirectoryごと消します。

```sh
rm -rf "$CANDIDATES/<版>"
```

`$CANDIDATES`自体は消しません。Caddyへ読み取り専用でmountしているので、無いと次の`homepage sync`や更新でCaddyを起動できません。

置いたことと消したことは、非公開の運用記録の現行公開環境の欄へ残します。
