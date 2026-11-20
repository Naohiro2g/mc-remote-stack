# Unity箱庭を配備する

Unity箱庭（`mc-remote-unity-sandbox`）は、状態とペアリングトークンをメモリ上だけに持つステートレスなコンテナです。次の 2 通りの方法で配備できます。

- **単体構成**：Docker Compose で `compose.yaml`・`Caddyfile`・`runtime/scratch.json` の 3 ファイルを配置して起動する最小構成
- **公開 VPS 同居構成**：既存の `mcrctl` 公開 VPS セット（`vps-server@20`／`unity-sandboxes@2`）に任意の数の Unity箱庭コンテナを追加する構成

前提となる Ubuntu ホストへの Docker 導入は [新しいUbuntu hostを準備する](fresh-host-bootstrap-guide_ja.md) を参照してください。

---

## 1. 単体で動かす（Docker Compose 直接利用）

Minecraft サーバーを持たず、GHCR のコンテナイメージを直接取得して Unity箱庭＋Scratch＋Bridge＋WireScope を立てる手順です。テンプレートは [`examples/unity-sandbox/`](../examples/unity-sandbox/compose.yaml) にあります。

### 1-1. 作業ディレクトリへファイルを揃える

```sh
mkdir -p ~/unity-sandbox/runtime ~/unity-sandbox/wirescope
cp examples/unity-sandbox/compose.yaml ~/unity-sandbox/compose.yaml
cp examples/unity-sandbox/Caddyfile ~/unity-sandbox/Caddyfile
cp examples/unity-sandbox/runtime/scratch.json ~/unity-sandbox/runtime/scratch.json
```

`~/unity-sandbox/wirescope/` には `minecraft-remote-tooling` リリースから取得した WireScope 静的アセット（`index.html` 等）を展開します。

### 1-2. ドメイン名とイメージタグを書き換える

`Caddyfile`・`compose.yaml`・`runtime/scratch.json` 内の `*.example.org` を自分のドメインへ置き換え、`unity-sandbox` のイメージタグ（例：`ghcr.io/naohiro2g/mc-remote-unity-sandbox:v2321.2.0`）を指定します。

- Unity箱庭の WebGL 画面は、ブラウザの現在ホストのルート `/ws/sync` へ WebSocket 接続します。そのためパス分け（`/unity/` 等）ではなく、**`unity-sb.example.org` のような独立したオリジン（ホスト名またはポート）** を割り当てます。
- 独自ドメインの公開 VPS では、Caddy が `80/443` で Let's Encrypt 証明書を自動取得します。
- LAN や Tailscale 内で試す場合は、`Caddyfile` の各サイトブロックをポート指定（`:8443`〜`:8446`）にして `compose.yaml` の Caddy `ports` を `127.0.0.1:8443-8446:8443-8446/tcp` へ変え、`tailscale serve` 等で HTTPS 終端します。

| 環境 | Caddy の待ち受け | `scratch.json` の URL |
| --- | --- | --- |
| 独自ドメイン公開 | `scratch.example.org` / `unity-sb.example.org`（80・443） | `https://.../`・`wss://...` |
| LAN / Tailscale | `:8443`〜`:8446`（loopback） | `https://<ts-host>:8443/`〜`:8446/` |

### 1-3. 起動と確認

```sh
cd ~/unity-sandbox
docker compose pull
docker compose up -d
docker compose ps
```

ブラウザで Unity箱庭（`https://unity-sb.example.org/`）と Scratch（`https://scratch.example.org/`）を開き、Scratch のペアリングコードを Unity箱庭側のチャット欄で `/mcremote pair NNN-NNN` として承認します。

更新するときはイメージタグを差し替えて再度 `docker compose pull && docker compose up -d` を実行します（永続ボリュームがないため、再起動後はワールドとペアリング状態が初期化されます）。

---

## 2. 公開 VPS に同居させる（`vps-server@20` / `unity-sandboxes@2`）

`mcrctl` で管理する公開 VPS（Minecraft＋ホームページ＋Scratch＋Bridge＋WireScope）へ、1 つまたは複数の独立した Unity箱庭コンテナを同居させます。

- Unity箱庭のイメージは PaperMC プリセットに縛らず、`operator/unity-sandboxes/sandboxes.toml` に `@sha256:` で固定指定します。
- 各 Unity箱庭は Docker 内部の `app` ネットワーク上で `--tcp-port 25575` として待ち受け、既存の Bridge からホスト名別名で選択して接続できます。`tcp_port` を省略した場合は外部非公開、`tcp_port`（例：LTS `25585`、最新版 `25586`）を指定した場合は `0.0.0.0:<tcp_port>:25575/tcp` で外部 TCP を公開します（ホストファイアウォール `ufw` と `mc-remote-docker-user` の許可が必要です）。
- `compose@22` では各 Unity箱庭コンテナへリソース隔離上限（`mem_limit: 512m` / `memswap_limit: 512m`、`cpus: 0.5`、`pids_limit: 256`、`ulimits.nofile: 1024`、ログ `10m×3`、`restart: unless-stopped`）を標準設定します。
- 更新トラックを分ける場合（例：最新版と長期固定版 LTS）は、それぞれ独立したコンテナとして並記します。

### 2-1. 運用環境を確認して入力ファイルを準備する

対象 deployment project へ移動し、まず運用環境を確認します。

```sh
mcrctl operator check --docker-context default
```

作業用の一時ディレクトリへ `sandboxes.toml` と更新用の `targets.toml` を用意します。外部 TCP を公開する場合は `tcp_port`（例：LTS を `25585`、最新版を `25586`）を各 `[[sandboxes]]` へ指定し、内部専用にする場合は省略します（過去の構成 `vps-server@19`／`unity-sandboxes@1` は `tcp_port` 非対応）。

```sh
REVIEW_DIR="$(mktemp -d "${TMPDIR:-/tmp}/mc-remote-unity.XXXXXX")"
cat <<'EOF' > "$REVIEW_DIR/sandboxes.toml"
[[sandboxes]]
id = "lts"
hostname = "unity-sb-lts.example.org"
tcp_port = 25585
image = "ghcr.io/naohiro2g/mc-remote-unity-sandbox:v2321.2.0@sha256:0000000000000000000000000000000000000000000000000000000000000000"

[[sandboxes]]
id = "latest"
hostname = "unity-sb.example.org"
tcp_port = 25586
image = "ghcr.io/naohiro2g/mc-remote-unity-sandbox:2400.0.0@sha256:1111111111111111111111111111111111111111111111111111111111111111"
EOF
```

`image` の `@sha256:...` には `docker buildx imagetools inspect` 等で確認した実際のダイジェストを指定します。公開ホスト名（`unity-sb.example.org`、`unity-sb-lts.example.org`）は、事前に DNS（CNAME または A レコード）で対象 VPS へ向けておきます。

あわせて既存の `operator/connection-targets/targets.toml` をベースに、Unity箱庭の接続先と各 Scratch オリジンの選択肢を追加します（[組別接続先の設定手順](scratch-connection-selections-guide_ja.md) も参照してください）。

```sh
cp operator/connection-targets/targets.toml "$REVIEW_DIR/targets.toml"
# $REVIEW_DIR/targets.toml 内の [[targets]] と [[selections]] に
# unity-sb.example.org / unity-sb-lts.example.org を追加する
```

### 2-2. 更新計画の生成・適用・確認

`vps-server@20` と現在適用中のプリセットを指定して更新 `plan` を作り、生成された `Caddyfile` と `compose.yaml` を確認した上で `apply` と `doctor` を実行します。

```sh
mcrctl deployment update plan \
  --to-profile vps-server@20 \
  --to-preset "<現在のexact preset>" \
  --replace-input "unity-sandboxes=$REVIEW_DIR/sandboxes.toml" \
  --replace-input "connection-targets=$REVIEW_DIR/targets.toml"
```

出力された `plan` ID を使って適用し、`doctor` でコンテナ稼働、各 `https://<hostname>/healthz` の正常応答（`OK doctor unity-sandbox=healthy count=<コンテナ数>`）、および `tcp_port` 指定コンテナへのローカル認証拒否応答（token 無し `hello` が `auth_required` を返すこと）を確認します。詳細な更新運用の流れは [VPSを新しいpresetへ更新する](public-vps-bootstrap-guide_ja.md) を参照してください。

```sh
PLAN_ID="sha256:<planが出力したID>"
mcrctl deployment update apply --plan-id "$PLAN_ID" --yes
mcrctl doctor
```
