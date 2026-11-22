# 2 系列 Web/Bridge 並設と外部 McRemote 接続先の手順（`vps-server@21`）

`vps-server@21`／`compose@23` では、1 台の公開 VPS 上で **最新版（`2400` 系・Scratch v2 契約）と LTS（`2321` 系・Scratch v1 契約）の 2 系列の Web/Bridge/WireScope** を並立させ、あわせて **有料 GAMEs 等の外部ホスト上で稼働する McRemote サーバー（TCP `25575` 等）へ公開 VPS の Bridge から中継** できます。

通常の更新手順の全体像は [既存 Public VPS 更新手順](public-vps-bootstrap-guide_ja.md)、組ごとの接続先一覧の基本は [組別接続先の設定手順](scratch-connection-selections-guide_ja.md) を参照してください。

---

## 1. `vps-server@21` で追加される仕組み

### 1-1. 外部接続先が DNS 切り替えだけでは動かない理由

`compose@22`（`vps-server@20`）以前は、すべての接続先が公開 VPS 内のコンテナであることを前提としていました。そのため、外部サーバーの DNS 名だけを切り替えても次の 2 点で外部へ到達できませんでした。

1. `connection-targets` に載った全ホスト名がローカル `minecraft` コンテナの `networks.app.aliases` に登録されるため、Bridge からの名前解決がローカルの別版 Minecraft コンテナに吸われる。
2. `bridge` コンテナが外部遮断された `app`（`internal: true`）ネットワークのみに所属しており、VPS 外の TCP `25575` へ発信できない。

`compose@23`（`connection-targets@5`）では、各ターゲットへ `host`・`port`・`external` を導入し、次のように解決します。

| 設定項目 | `external = false`（既定・VPS 内コンテナ） | `external = true`（GAMEs 等の外部サーバー） |
| --- | --- | --- |
| ローカル `minecraft` aliases | Unity 以外のローカル host 名を登録 | **登録しない**（外部 DNS を参照） |
| `bridge` / `bridge-lts` の network | `["app"]` | **`{"app": {}, "egress": {"gw_priority": 1}}`**（外向け発信許可） |
| `mcrctl doctor` | ローカルコンテナ・公開ポートを検査 | **外部 `host:port` へ `hello` を送り `auth_required` を検証** |

> [!NOTE]
> `external = false` のローカルコンテナ接続先は内部ポート `25575` に統一されます（Unity 箱庭も内部 `app` 上は `--tcp-port 25575` で待ち受けます）。`external = true` では `25575` 以外の任意のポートも `v2` 系列の Bridge で指定できます。

### 1-2. Scratch v2 契約と `lts-web@1` の役割分担

- **主系統（`public-routes@3`＋`preset`）**:
  - `2400` 系プリセット（Scratch v2 契約・ID ルーティング対応 Bridge）を使うと、Renderer は `runtime/scratch.json`（および追加組 `runtime/web-pairs/<scratch>.json`）を **`schema_version: 2`（`default_target_id`＋`host`/`port`）** で生成し、Bridge 用に `runtime/bridge-targets.json` を書き出して **`BRIDGE_TARGETS_FILE`** で渡します（旧環境変数 `BRIDGE_SANDBOX_*` は設定しません）。
- **並立 LTS 系統（任意・`operator/lts-web/lts.toml` / `lts-web@1`）**:
  - `lts-web@1` を追加すると、別の `preset`（`2321` 系の公開済みプリセット）・別のホスト名 3 点セット（`scratch` / `bridge` / `wirescope`）・独自の `target_ids` / `default_target` で、独立した **`scratch-lts`**・**`bridge-lts`**・**`/srv/wirescope-lts`**・`runtime/scratch-lts.json` を同じ Compose と Caddy 上に構築します。
  - `2321` 系プリセット（v1 契約）の LTS 系統では、従来どおり `schema_version: 1` と `BRIDGE_SANDBOX_ALLOWLIST` / `BRIDGE_SANDBOX_PORT=25575` を出力します。

---

## 2. Stack の外で事前に用意するもの（DNS・有料 GAMEs）

`mcrctl deployment update plan` / `apply` の前に、Stack 管理外の作業を完了させます。

1. **DNS レコードの用意**:
   - Caddy が Let's Encrypt 証明書を取得するため、新しく並立させるホスト名（例: `scratch-lts.<domain>` / `bridge-lts.<domain>` / `wirescope-lts.<domain>`）を、あらかじめ公開 VPS の IP へ向けておきます。
   - 外部 McRemote サーバーのホスト名（例: `sb.<domain>` または `sb-lts.<domain>`）は、有料 GAMEs 側のアドレスへ向けておきます（公開 VPS のローカル `minecraft` aliases と重複させないこと）。
2. **有料 GAMEs 側の構築・移行（人間オペレーター作業）**:
   - 対象サーバーへワールド・LuckPerms DB・Paper・周辺プラグイン・対象版の McRemote プラグイン JAR を配置します。
   - **McRemote の設定（`plugins/McRemote/config.yml`）**: `session-only`（認証必須）および公開 TCP ポート（例: `25575`）が有効で、外部（少なくとも公開 VPS）から TCP 接続できることを確認します。古い認証データは引き継がず、移行先では利用者に再ペアリングしてもらいます。

---

## 3. 入力ファイルの例（11 月末の予行演習 → 12 月 1 日の本番構成）

`operator/connection-targets/targets.toml`（`connection-targets@5`）では、各接続先の `host`（後方互換の `sandbox` も可）・`port`（既定 `25575`）・`external`（既定 `false`）と、主系統側の各 Scratch オリジンの選択肢 `[[selections]]` を定義します。major 版が異なるクライアントとサーバーは `hello` で拒否されるため、**組ごとに同じ系列（`2400` 同士または `2321` 同士）だけ** を並べます。

### 3-1. 11 月中の予行演習（主系統＝`2400` rc1 on `-beta`、LTS＝`2321` on 無印・GAMEs）

`operator/public-routes/routes.toml`（主系統を `-beta` のみで動かす例）:

```toml
homepage = "mc-remote.example"
homepage_aliases = ["www.mc-remote.example"]
scratch = "scratch-beta.mc-remote.example"
bridge = "bridge-beta.mc-remote.example"
minecraft = "sb-beta.mc-remote.example"
wirescope = "wirescope-beta.mc-remote.example"
web_pairs = []
```

`operator/connection-targets/targets.toml`（`connection-targets@5`）:

```toml
[[targets]]
id = "beta"
label = "公開ベータ（2400 rc1）"
host = "sb-beta.mc-remote.example"
port = 25575
external = false

[[targets]]
id = "unity-latest"
label = "Unity箱庭（2400 rc1）"
host = "unity-sb.mc-remote.example"
port = 25575
external = false

[[targets]]
id = "stable"
label = "標準（Minecraft・GAMEs）"
host = "sb.mc-remote.example"
port = 25575
external = true

[[targets]]
id = "unity-lts"
label = "Unity箱庭（LTS）"
host = "unity-sb-lts.mc-remote.example"
port = 25575
external = false

[[notices]]
heading = "お知らせ"
body = "今回の運用者お知らせを記入してください。"

[[selections]]
scratch = "scratch-beta.mc-remote.example"
target_ids = ["beta", "unity-latest"]
default_target = "beta"
```

`operator/lts-web/lts.toml`（`lts-web@1`）:

```toml
preset = "public-web-paper@18"
scratch = "scratch.mc-remote.example"
bridge = "bridge.mc-remote.example"
wirescope = "wirescope.mc-remote.example"
target_ids = ["stable", "unity-lts"]
default_target = "stable"
```

### 3-2. 12 月 1 日本番構成（主系統＝`2400.0.0` on 無印＋`-beta` 保険、LTS＝`2321` on `-lts`・GAMEs）

本番切替時は、主系統の `routes.toml` へ `scratch`（主）と `scratch-beta`（`[[web_pairs]]` 保険）を並べ、主系統 `[[selections]]` の両方に `["stable", "unity-latest"]`（ここで `stable` が VPS の `sb`）、`lts.toml` 側を `-lts` ホスト群＋GAMEs `sb-lts`（`external = true`）＋`unity-sb-lts` へ切り替えます。

`operator/public-routes/routes.toml`:

```toml
homepage = "mc-remote.example"
homepage_aliases = ["www.mc-remote.example"]
scratch = "scratch.mc-remote.example"
bridge = "bridge.mc-remote.example"
minecraft = "sb.mc-remote.example"
wirescope = "wirescope.mc-remote.example"

[[web_pairs]]
scratch = "scratch-beta.mc-remote.example"
wirescope = "wirescope-beta.mc-remote.example"
```

`operator/connection-targets/targets.toml`:

```toml
[[targets]]
id = "stable"
label = "標準（Minecraft）"
host = "sb.mc-remote.example"
port = 25575
external = false

[[targets]]
id = "unity-latest"
label = "Unity箱庭"
host = "unity-sb.mc-remote.example"
port = 25575
external = false

[[targets]]
id = "lts"
label = "長期固定（LTS・GAMEs）"
host = "sb-lts.mc-remote.example"
port = 25575
external = true

[[targets]]
id = "unity-lts"
label = "Unity箱庭（LTS）"
host = "unity-sb-lts.mc-remote.example"
port = 25575
external = false

[[notices]]
heading = "お知らせ"
body = "今回の運用者お知らせを記入してください。"

[[selections]]
scratch = "scratch.mc-remote.example"
target_ids = ["stable", "unity-latest"]
default_target = "stable"

[[selections]]
scratch = "scratch-beta.mc-remote.example"
target_ids = ["stable", "unity-latest"]
default_target = "stable"
```

`operator/lts-web/lts.toml`:

```toml
preset = "public-web-paper@18"
scratch = "scratch-lts.mc-remote.example"
bridge = "bridge-lts.mc-remote.example"
wirescope = "wirescope-lts.mc-remote.example"
target_ids = ["lts", "unity-lts"]
default_target = "lts"
```

---

## 4. 切替の実施手順・時間見込み・ロールバック

12 月 1 日 0 時（JST）の切替は、デプロイからホームページ更新まで **3 時間以内** に完了させる前提です。以下は通常更新（`mcrctl deployment update`）を軸にした手順と目安時間です。

| 順序 | 作業内容 | 主なコマンド・確認 | 時間目安 |
| --- | --- | --- | --- |
| 1 | 事前準備（Stack 外・前日まで可） | DNS 反映（`sb` / `*-lts`）、GAMEs 側 McRemote 稼働、対象 VPS へ事前 `docker pull`・新規入力ファイルの review copy 作成 | 事前完了 |
| 2 | メンテナンス開始・環境確認 | `mcrctl operator check` | 約 2 分 |
| 3 | 更新計画生成と候補確認 | `mcrctl deployment update plan --to-profile vps-server@21 --to-preset "<2400 exact preset>" --replace-input ...`（生成物 `runtime/scratch*.json`・`bridge-targets.json`・`compose.yaml`・`Caddyfile` を確認） | 約 5 分 |
| 4 | 更新適用と起動後 `doctor` | `mcrctl deployment update apply --plan-id "sha256:<ID>" --yes`（image pull・切替・起動待機・`doctor` まで実行） | 約 5〜10 分 |
| 5 | `mcrctl doctor` 単体再確認 | `OK doctor lts-web=current` と `OK doctor external-targets=healthy count=1` を確認 | 約 2 分 |
| 6 | 実ブラウザ・権限モード確認 | 両系列（`scratch`・`scratch-lts`）での接続／WireScope／お知らせ表示、および後述の `0600`/`0700` 確認 | 約 15 分 |
| 7 | ホームページ更新・最終確認 | [ホームページ更新手順](homepage-sync-guide_ja.md)（`mcrctl homepage sync`）および公開記事／告知確認 | 約 30 分 |

### 実行コマンド例（プロジェクト外に作成した review copy を渡す）

```sh
cd "<対象deployment project>"
mcrctl operator check

REVIEW_DIR="$(mktemp -d "${TMPDIR:-/tmp}/mc-remote-dual.XXXXXX")"
# $REVIEW_DIR/routes.toml, targets.toml, lts.toml, plugins.toml を用意して確認する

mcrctl deployment update plan \
  --to-profile vps-server@21 \
  --to-preset "<対象2400 exact preset>" \
  --replace-input "public-routes=$REVIEW_DIR/routes.toml" \
  --replace-input "connection-targets=$REVIEW_DIR/targets.toml" \
  --replace-input "lts-web=$REVIEW_DIR/lts.toml" \
  --replace-input "minecraft-plugins=$REVIEW_DIR/plugins.toml"
```

出力された `PLAN deployment-update id=sha256:...` を確認したら、候補ディレクトリ（`.mcrctl/updates/<id>/candidate/generated/`）内の `runtime/scratch.json`（`schema_version: 2`）、`runtime/scratch-lts.json`（`schema_version: 1`）、`runtime/bridge-targets.json`、`compose.yaml`（`bridge-lts` の `egress` と `minecraft` の `aliases`）を検査してから適用します。

```sh
PLAN_ID="sha256:<planが出力したID>"
mcrctl deployment update apply --plan-id "$PLAN_ID" --yes
mcrctl doctor
```

`mcrctl doctor` では、従来の行に加えて次の 2 行が出力されます。

- `OK doctor lts-web=current`（`lts-web@1` 導入時）
- `OK doctor external-targets=healthy count=<外部接続先数>`（外部ターゲット選択時）

### 失敗時のロールバック手順

1. **`apply` 中の失敗（`docker compose up` または起動後 `doctor` 失敗）**:
   - `mcrctl deployment update apply` が自動的に元の `compose.yaml`・生成物（`source projection`）を起動し直して復帰します。まず `mcrctl doctor` で現行系が正常に復旧していることを確認してください。
2. **適用完了後に実機試験で重大問題が見つかった場合**:
   - 一時作業ディレクトリは使わず、切替前の入力（または `lts-web` 無しの `vps-server@20` / 直前プリセット・直前 `targets.toml`・`routes.toml`）を指定して再度 `mcrctl deployment update plan` → `apply` を実行して戻します（新規設定の追加・削除を行わない場合でも、プロファイル後退は禁止されているため `vps-server@21` 上で入力ファイルを切替前の内容に戻すか、旧構成の別プロジェクトへ戻します）。
   - DNS（`sb` 等）を外部へ向け直していた場合は、DNS と合わせて戻します。

---

## 5. 公開後の McRemote credential 権限確認（`0600` / `0700`）

最新系（公開 VPS `sb`）と LTS 系（有料 GAMEs `sb-lts`）の両方で、McRemote のセッション認可ディレクトリと credential ファイルのモードが最小権限になっていることを確認します。

### 5-1. 公開 VPS（`sb`・Docker コンテナ）の確認

```sh
docker exec "$(docker compose -f "<対象deployment project>/generated/compose.yaml" ps -q minecraft)" \
  stat -c '%a %u:%g %n' /data/plugins/McRemote/session-only /data/plugins/McRemote/session-only/*
```

- ディレクトリが **`700`**、ファイルが **`600`**（かつ運用 UID/GID `1000:1000` 等）であることを確認します。

### 5-2. 有料 GAMEs（`sb-lts`・SSH シェル無し／SFTP・パネルのみ）の確認

有料 GAMEs 環境には SSH シェルが無いため、SFTP クライアントまたは管理パネルのファイルマネージャで `plugins/McRemote/session-only/` を確認します。

```sh
# 対話 SFTP で接続後（例: sftp -P <port> <user>@<games-host>）に確認するコマンド
#   ls -ln plugins/McRemote
#   ls -ln plugins/McRemote/session-only
```

- `session-only` ディレクトリが `drwx------`（`0700`）、配下の設定・credential ファイルが `-rw-------`（`0600`）であることを確認します。パネルのファイルマネージャでも同じ権限表記（`0700` / `0600`）を確認できます。
