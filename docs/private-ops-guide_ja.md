# private opsリポで運用情報を管理する

`mc-remote-stack` は公開できる共通手順と仕組みを持ち、個々の運用者の接続先・DNS・稼働中セットの状態などの非公開情報は、運用者自身の **private ops リポジトリ**（公式運用では非公開リポ `mc-remote-backstage`）に置きます。役割分担の背景は knowledge の[release運用と責務分担](https://github.com/Naohiro2g/mc-remote-knowledge/blob/main/00-hub/release-operations-responsibility-design_ja.md#12-private-opsリポoss利用者と公式)を参照してください。

この仕組みでは細かい書式強制や禁止リストを増やさず、運用者や担当エージェントが状況に合わせて主体的に整理できる最小構成だけを揃えます。守るべき絶対の境界は **「秘密実値（トークン・パスワード・秘密鍵・未マスク生ログ・バックアップ実体）を Git に残さない」** の一点です。

---

## 1. 最小構成で立ち上げる

公開テンプレートリポジトリ（GitHub の Use this template 等）は使わず、手元の管理端末へ [`examples/private-ops/`](../examples/private-ops/README.md) をコピーして**非公開（private）リポジトリ**として初期化します。

```sh
PRIVATE_OPS_DIR="$HOME/mc-remote-private-ops"
cp -R examples/private-ops "$PRIVATE_OPS_DIR"
cd "$PRIVATE_OPS_DIR"
git init -b main
git add .gitignore README.md AGENTS.md inventory.md deployments/README.md
git commit -m "chore: initialize private ops repository"
```

リモートへ push する前に、作成先リポジトリが **Private** であることを確認してから紐付けます（確認できない場合は公開設定を確かめるまで push しません）。

```sh
git remote add origin "<お使いのprivateリポジトリURL>"
git push -u origin main
```

### 同梱ファイルの役割

| ファイル・ディレクトリ | 役割 |
| --- | --- |
| [`inventory.md`](../examples/private-ops/inventory.md) | 管理しているホスト、DNS ルーティング、稼働中／片付け待ち deployment の一覧 |
| [`deployments/`](../examples/private-ops/deployments/README.md) | 各ホスト上の `mc-remote.toml`・`mc-remote.lock.toml`・`operator/`（単体 Compose 構成は `compose.yaml`・`Caddyfile` 等）の非秘密な構成控え |
| [`.gitignore`](../examples/private-ops/.gitignore) | `.env*`・鍵ファイル・バックアップ実体・生ログ等を誤って commit しないための除外設定 |
| [`AGENTS.md`](../examples/private-ops/AGENTS.md) | 担当エージェント向けの短い作業境界 |

---

## 2. 日々の運用の進め方

### 台帳には「いま動いている状態」だけを置く

`inventory.md` は過去の作業日誌（事件簿）にせず、**「今どのホストで何が動いており、一時検証セットをいつ片付けるか」** がひと目で分かる状態に保ちます。時系列の経緯は Git のコミット履歴に任せ、残したい調査メモや移行計画があれば別ファイルへ分けてリンクします。

### 設定控えを正にして二重転記を減らす

プラグイン一覧や lock SHA を Markdown の文章へ手作業で書き写し続けると実機とずれやすくなります。運用変更の完了後にホストから非秘密の宣言ファイルを控えとして `deployments/<project>/` へ回収しておけば、現在状態の正本になると同時に、別ホストへの移設・再構築時にもそのまま参照できます。

`mcrctl` の入力ファイルはパスワード等の秘密実値をホスト側の secret store へ分離しているため通常そのまま保存できますが、commit 前に `git diff` を確認して秘密実値が紛れ込んでいないか確かめます。

```sh
HOST="vps1"
PROJECT="public-main"
mkdir -p "deployments/$PROJECT"
scp "$HOST:~/mc-remote-deployments/$PROJECT/mc-remote.toml" \
    "$HOST:~/mc-remote-deployments/$PROJECT/mc-remote.lock.toml" \
    "deployments/$PROJECT/"
scp -r "$HOST:~/mc-remote-deployments/$PROJECT/operator" "deployments/$PROJECT/"
```

### 管理端末を起点に作業する

本番・公開ホスト上へ private ops リポジトリ全体を clone しておくと、万一そのホストが侵害された際に他ホストの情報や全体構成まで読まれてしまいます。private ops リポジトリは手元の管理端末に置き、ホストにはそのホストで動かす deployment に必要な入力ファイルだけを渡します。

また、台帳の記述をそのまま現在状態とみなさず、作業前に必ず対象ホストの実状態（[公開VPS更新](public-vps-bootstrap-guide_ja.md) の `mcrctl operator check`・`mcrctl doctor` 等）を確かめ、完了後に `deployments/` と `inventory.md` を更新します。

### 運用実態に合わせて育て、汎用知見は還元する

契約・費用メモ、バックアップ運用メモ、追加ホスト群の整理方法などは、運用者や担当エージェントの裁量で自由に追加・再構成して構いません。一方で「別の環境・他の OSS 利用者でもそのまま役立つ手順や自動化」が見つかった場合は、private 情報を除いた形でこの `mc-remote-stack` リポジトリへ還元してください。
