# Fresh host bootstrap guide

fresh な Ubuntu 系 host を、安全に `mc-remote-stack` の検証・生成を始められる地点まで運ぶ。
provider、実 IP、個人名、秘密値には依存しない。

## 現在の実装境界

現行の vertical slice は deployment project の `init`、`validate`、`repo check`、`plan`、
EULA gate、`resolve`、`artifact fetch`、`render`に加え、isolated `home-beta`の初回
bootstrap applyまでを実装している。既存world import、upgrade、複数project transaction、
firewall変更は実装していない。この文書も **production readinessやprotocol compatibilityを
bootstrap applyだけから主張しない**。

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
command -v docker
docker context inspect default
docker --context default version
docker --context default compose version
```

- Python は `3.11` 以上。
- 初回applyは対象host上のlocal Unix socket Docker contextだけを受理する。
- Docker Engine / Compose v2の導入方法、OS package、firewallはdistribution、provider、
  選択profileに合わせて確認する。`mcrctl apply`は自動installしない。
- port を旧 runbook から一括で開けない。公開 port と到達範囲は、生成する topology と認証境界を確認して決める。
- token、password、秘密鍵を clone や deployment project に置かない。
- Docker socketへのwrite accessはhost root相当の権限境界として扱い、個人管理者userへ限定する。

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

## 5. isolated `home-beta` project

```bash
uv run mcrctl init ./deployments/home-beta \
  --format toml \
  --deployment-name home \
  --profile home-server@2 \
  --environment-identity home-beta \
  --channel beta \
  --exposure isolated \
  --purpose integration \
  --preset mcremote-paper@1 \
  --artifact-store /var/lib/mc-remote/artifacts \
  --volume minecraft-data=home-beta-minecraft-data \
  --world-identity home-beta-world \
  --bind-address 127.0.0.1 \
  --java-port 25565 \
  --mcremote-port 25575
uv run mcrctl validate --project ./deployments/home-beta
uv run mcrctl repo check --project ./deployments/home-beta
uv run mcrctl accept-eula --project ./deployments/home-beta --yes
```

directory名からaxisやidentityを推測しない。`home-alpha`は同じfileへ追加せず、後から別project、
別volume、別worldとして作る。

bundled `mcremote-paper@1`は最初のlive evidence前なのでunverifiedである。bootstrapする場合だけ
`mc-remote.toml`に人間が具体的理由を記録する。

```toml
[acknowledgements]
allow_unverified = true
unverified_reason = "initial isolated home-beta live evidence"
allow_eol = false
eol_reason = ""
```

order内の理由だけでは足りない。resolve時にもone-shot flagを渡し、生成前にplanをreviewする。

```bash
uv run mcrctl resolve \
  --project ./deployments/home-beta \
  --allow-unverified
uv run mcrctl plan --project ./deployments/home-beta
uv run mcrctl artifact fetch --project ./deployments/home-beta
uv run mcrctl render \
  --project ./deployments/home-beta \
  --output ./deployments/home-beta/generated
```

unverified警告がある`plan`は内容を表示してstatus 1を返す。lock identity、profile / preset digest、
artifact、bind port、volume、worldを人間が確認する。秘密値はdeployment projectに保存しない。

## 6. bootstrap apply

reviewしたlock identityを手入力し、同じtarget host上の明示local Docker contextへ適用する。

```bash
REVIEWED_LOCK_IDENTITY="sha256:<planで確認した64-hex>"
uv run mcrctl apply \
  --project ./deployments/home-beta \
  --output ./deployments/home-beta/generated \
  --expected-lock-identity "$REVIEWED_LOCK_IDENTITY" \
  --docker-context default \
  --bootstrap \
  --yes \
  --allow-unverified
```

applyはcurrent lockとcanonical renderを再検証し、未知container / volume、port衝突を拒否する。
exact OCI image pull後にmanaged external volumeを作り、Composeのrunning / healthy待ちとlock label
postcheckを行う。起動失敗ではcontainerをdownするが、world volumeは削除しない。

詳細は[`home-beta` bootstrap apply設計](home-beta-bootstrap-apply-design_ja.md)を正とする。

## 7. 現在の停止点

bootstrap applyの次はlive evidenceである。次が閉じる前に、isolated betaのcontainer起動を
一般production、公開network、upgrade可能、compatibility verifiedと読み替えない。

- deployment doctor / protocol `live-auto` smoke
- backup / restore の実機検証
- upgrade / rollback のdeployed-state transaction
- provider firewall と host firewall の責任分界
- 複数projectのhost-level collision transaction

秘密を含むraw logはGit外、private host / inventoryは`mc-remote-backstage`、公開可能な
sanitized live evidenceはknowledge `14-evidence`へ分ける。

## 旧 runbook から carry した耐久原則

- 人間の SSH 操作は管理者ごとの個人ユーザーに分ける。
- root は初期 bootstrap と emergency recovery に限定する。
- SSH と `sudo` を別 session で確認してから root/password login を閉じる。
- 秘密値を Git に入れない。
- 観測済みの host 状態を、別 host の desired state とみなさない。

旧 runbook の per-service systemd user、`/opt/<service>/releases`、package Caddy、手動 symlink deploy は
現行 stack の構成と競合するため carry していない。
