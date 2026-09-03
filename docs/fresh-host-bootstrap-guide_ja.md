# Fresh Ubuntu host bootstrap runbook

このrunbookは、新しいUbuntu hostをMcRemoteのdeployment operator環境へ準備する一回の正準手順である。
完了後は[public VPS release deployment runbook](public-vps-bootstrap-guide_ja.md)へ進む。

## 1. bootstrap handoffを受け取る

backstage／Stack handoffから次を受け取る。

| 値 | 内容 |
| --- | --- |
| `ADMIN_USER` | 個人管理者のlogin名 |
| `AUTHORIZED_KEYS` | 管理者の公開鍵file |
| `MC_REMOTE_TARGET` | SSH接続先 |
| `MC_REMOTE_STACK_REF` | review済みStack commitを含むremote ref |
| `MC_REMOTE_STACK_COMMIT` | review済みStack exact commit |

provider consoleのroot sessionを、管理者SSHの確認が終わるまで維持する。

## 2. 個人管理者を作る

root sessionでhandoff値を設定して実行する。

```sh
ADMIN_USER="<handoffの個人管理者>"
AUTHORIZED_KEYS="<handoffの公開鍵file>"

adduser "$ADMIN_USER"
usermod -aG sudo "$ADMIN_USER"
install -d -m 700 -o "$ADMIN_USER" -g "$ADMIN_USER" "/home/$ADMIN_USER/.ssh"
install -m 600 -o "$ADMIN_USER" -g "$ADMIN_USER" "$AUTHORIZED_KEYS" \
  "/home/$ADMIN_USER/.ssh/authorized_keys"
```

管理端末の別terminalから接続と管理権限を確認する。

```sh
MC_REMOTE_TARGET="<handoffのSSH接続先>"
ssh "$MC_REMOTE_TARGET"
sudo -v
```

## 3. SSHを公開鍵loginへ固定する

root sessionで先頭評価されるdrop-inを配置し、実効設定まで確認する。

```sh
install -d -m 755 /etc/ssh/sshd_config.d
printf '%s\n' \
  'PermitRootLogin no' \
  'PasswordAuthentication no' \
  > /etc/ssh/sshd_config.d/00-mc-remote-bootstrap.conf
sshd -t
systemctl reload ssh
sshd -T | awk '
$1=="permitrootlogin" ||
$1=="passwordauthentication" ||
$1=="pubkeyauthentication" {
  print
}'
```

実効値は次の一組になる。

```text
permitrootlogin no
passwordauthentication no
pubkeyauthentication yes
```

管理端末から新しいSSH sessionを開き、`sudo -v`まで確認する。

## 4. exact Stack checkoutを用意する

個人管理者のsessionで実行する。

```sh
MC_REMOTE_STACK="$HOME/mc-remote-stack"
MC_REMOTE_STACK_REF="<handoffのremote ref>"
MC_REMOTE_STACK_COMMIT="<handoffのexact commit>"

git clone https://github.com/Naohiro2g/mc-remote-stack.git "$MC_REMOTE_STACK"
git -C "$MC_REMOTE_STACK" fetch origin "$MC_REMOTE_STACK_REF"
git -C "$MC_REMOTE_STACK" switch --detach "$MC_REMOTE_STACK_COMMIT"
test "$(git -C "$MC_REMOTE_STACK" rev-parse HEAD)" = "$MC_REMOTE_STACK_COMMIT"
```

## 5. operator toolchainを構築する

checkoutに同梱されたbootstrapを実行する。

```sh
"$MC_REMOTE_STACK/tools/bootstrap-ubuntu-operator.sh" --install
```

bootstrapはUbuntuのsupport対象versionを確認し、固定versionの`uv`を
`$HOME/.local/bin/uv`へ配置する。続いてPython 3.11、Docker Engine、Compose、checkoutの`.venv`を
準備し、個人管理者へDocker accessを設定する。`/var/lib/mc-remote`が専用runtime groupで管理される
hostでは、そのgroup membershipも同時に設定する。

install完了後に一度logoutし、新しいSSH sessionで確認する。

```sh
MC_REMOTE_UV="$HOME/.local/bin/uv"
MC_REMOTE_STACK="$HOME/mc-remote-stack"

test -x "$MC_REMOTE_UV"
"$MC_REMOTE_STACK/tools/bootstrap-ubuntu-operator.sh" --check
"$MC_REMOTE_UV" run --project "$MC_REMOTE_STACK" mcrctl --help
```

成功時は次の二行が含まれる。

```text
OK operator bootstrap tools=ready uv=/home/<operator>/.local/bin/uv docker-access=direct compose=<version>
OK repo environment=/home/<operator>/mc-remote-stack/.venv
```

## 6. deployment runbookへ進む

host bootstrapの返却値は次の一組である。

```text
target: <backstage上の参照>
operator: <ADMIN_USER>
stack checkout: <MC_REMOTE_STACK>
stack commit: <MC_REMOTE_STACK_COMMIT>
uv: /home/<operator>/.local/bin/uv
docker context: default
operator bootstrap: ready
```

この値をpublic VPS deployment handoffへ入れ、
[public VPS release deployment runbook](public-vps-bootstrap-guide_ja.md)の`mcrctl operator check`から続行する。
