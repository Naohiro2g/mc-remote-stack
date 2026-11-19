# Deployment 構成ファイルの控え

稼働中（または復帰用に保持している）deployment の非秘密な設定ファイルを、プロジェクト名ごとのディレクトリへ控えとして保存します。

## 配置するファイルの目安

- **`mcrctl` 管理のセット**:
  - `mc-remote.toml`
  - `mc-remote.lock.toml`
  - `operator/` 配下の各種 `.toml` ファイル
- **単体 Docker Compose のセット**:
  - `compose.yaml`、`Caddyfile`、`runtime/*.json`

`mcrctl` の設定ファイルはパスワード等の秘密実値を外部 secret store 参照に分離しているため、通常はそのまま控えとして保存できます（手編集した設定に秘密実値を書いた場合は Git に入れないでください）。  
ホスト固有の Docker volume（world・Caddy TLS データ等）や secret store は Git 外のバックアップ手順で管理します。
