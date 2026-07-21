# Fresh host bootstrap guide

fresh な Ubuntu 系 host を、安全に `mc-remote-stack` の検証・生成を始められる地点まで運ぶ。
provider、実 IP、個人名、秘密値には依存しない。

## 現在の実装境界

現行の vertical slice は deployment project の `init`、`validate`、`repo check`、`plan`、
EULA gate、`render` までを実装している。生成物を host へ適用する機能はまだ実装していない。
この文書も **host への production apply 完了を主張しない**。

## 1. 最初の個人管理者ユーザー

初期 root session は、個人管理者ユーザーを作るところまでに限定する。共有ログインユーザーを作らない。

```bash
ADMIN_USER=alice
adduser "$ADMIN_USER"
usermod -aG sudo "$ADMIN_USER"
install -d -m 700 -o "$ADMIN_USER" -g "$ADMIN_USER" "/home/$ADMIN_USER/.ssh"
install -m 600 -o "$ADMIN_USER" -g "$ADMIN_USER" authorized_keys \
  "/home/$ADMIN_USER/.ssh/authorized_keys"
```

別 terminal から SSH と `sudo -v` を確認するまで、既存 root session を閉じない。

```bash
ssh alice@server.example.com
sudo -v
```

## 2. SSH hardening

個人管理者ユーザーの別 session を確認した後だけ、root login と password login を閉じる。

```bash
sudo install -d -m 755 /etc/ssh/sshd_config.d
printf '%s\n' \
  'PermitRootLogin no' \
  'PasswordAuthentication no' \
  | sudo tee /etc/ssh/sshd_config.d/99-mc-remote-bootstrap.conf
sudo sshd -t
sudo systemctl reload ssh
```

reload 後も別 terminal で SSH と `sudo -v` を再確認する。失敗した session を唯一の管理経路にしない。

## 3. host と toolchain の preflight

最低限、次を確認する。

```bash
command -v git
command -v python3
python3 --version
command -v uv
```

- Python は `3.11` 以上。
- OS package、container runtime、firewall は provider と選択 profile に合わせて確認する。
- port を旧 runbook から一括で開けない。公開 port と到達範囲は、生成する topology と認証境界を確認して決める。
- token、password、秘密鍵を clone や deployment project に置かない。

## 4. package の取得と自己検証

```bash
git clone https://github.com/Naohiro2g/mc-remote-stack.git
cd mc-remote-stack
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run mcrctl --help
```

ここで失敗したら deployment project を作らず、toolchain または repository の状態を直す。

## 5. deployment project の検証・生成

```bash
uv run mcrctl init ./deployment --profile official-vps
uv run mcrctl validate --project ./deployment
uv run mcrctl repo check --project ./deployment
uv run mcrctl plan --project ./deployment
```

`init` は `mc-remote.yml`、`mc-remote.lock.yml`、`secrets.example.yml` と project README を作る。
初回 `plan` が EULA 同意や immutable artifact identity の不足で停止するのは正常な gate である。
診断に従って desired state をレビューし、lock の placeholder を検証済みの digest / SHA-256 へ解決する。
未解決 selector を production 値として補完しない。

EULA を確認して lock を解決した後、gate を再実行してから生成する。

```bash
uv run mcrctl accept-eula --project ./deployment --yes
uv run mcrctl plan --project ./deployment
uv run mcrctl render --project ./deployment --output ./deployment/generated
```

秘密値は `secret://...` 参照と `mcrctl secret set` を使い、deployment project に保存しない。

## 6. 停止点

現行版では `render` 後に自動で host へ apply しない。次の内容が実装・検証されるまでは、生成物を
production へ手作業で写して「公式手順」としない。

- fresh-host dependency の検出と導入
- apply / upgrade / rollback の transaction boundary
- health / doctor と失敗時 rollback
- backup / restore の実機検証
- provider firewall と host firewall の責任分界

## 旧 runbook から carry した耐久原則

- 人間の SSH 操作は管理者ごとの個人ユーザーに分ける。
- root は初期 bootstrap と emergency recovery に限定する。
- SSH と `sudo` を別 session で確認してから root/password login を閉じる。
- 秘密値を Git に入れない。
- 観測済みの host 状態を、別 host の desired state とみなさない。

旧 runbook の per-service systemd user、`/opt/<service>/releases`、package Caddy、手動 symlink deploy は
現行 stack の構成と競合するため carry していない。
