# Fresh Ubuntu host bootstrap runbook

このrunbookは、新しいUbuntu hostをMcRemoteのdeployment operator環境へ準備する一回の正準手順である。
完了後は[public VPS release deployment runbook](public-vps-bootstrap-guide_ja.md)へ進む。

## 1. bootstrap handoffを受け取る

backstage／Stack handoffから次を受け取る。

| 値 | 内容 |
| --- | --- |
| `ADMIN_USER` | 個人管理者のlogin名 |
| `AUTHORIZED_KEYS` | 管理者の公開鍵file |
| SSH接続先 | 対象host |
| remote ref | review済みStack commitを含むremote ref |
| exact commit | review済みStack commit |

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
ssh "<handoffのSSH接続先>"
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

Stack checkoutの場所は`~/mc-remote-stack`に固定する。

```sh
git clone https://github.com/Naohiro2g/mc-remote-stack.git ~/mc-remote-stack
cd ~/mc-remote-stack
git fetch origin "<handoffのremote ref>"
git switch --detach "<handoffのexact commit>"
```

## 5. operator toolchainを構築する

checkoutに同梱されたbootstrapを実行する。

```sh
~/mc-remote-stack/tools/bootstrap-ubuntu-operator.sh --install
```

bootstrapはUbuntuのsupport対象versionを確認し、固定versionの`uv`を`$HOME/.local/bin/uv`へ配置する。
続いてDocker Engine、Compose、checkoutの`.venv`（Pythonは`.python-version`の3.12で、UbuntuのPythonを使う）を
準備し、`uv`と`mcrctl`を`/usr/local/bin`へlinkして、どこからでもcommand名だけで実行できるようにする。
個人管理者へDocker accessも設定する。`/var/lib/mc-remote`が専用runtime groupで管理される
hostでは、そのgroup membershipも同時に設定する。

install完了後に一度logoutし、新しいSSH sessionで確認する。

```sh
~/mc-remote-stack/tools/bootstrap-ubuntu-operator.sh --check
mcrctl --help
```

成功時は次の二行が含まれる。

```text
OK operator bootstrap tools=ready uv=/home/<operator>/.local/bin/uv docker-access=direct compose=<version>
OK repo environment=/home/<operator>/mc-remote-stack/.venv python=3.12 mcrctl=/usr/local/bin/mcrctl
```

## 6. deployment runbookへ進む

host bootstrapの返却値は次の一組である。

```text
target: <backstage上の参照>
operator: <ADMIN_USER>
stack checkout: ~/mc-remote-stack
stack commit: <handoffのexact commit>
uv: /home/<operator>/.local/bin/uv
mcrctl: /usr/local/bin/mcrctl
docker context: default
operator bootstrap: ready
```

この値をpublic VPS deployment handoffへ入れ、
[public VPS release deployment runbook](public-vps-bootstrap-guide_ja.md)の`mcrctl operator check`から続行する。

## PATHが通っていないとき

作業中に`mcrctl: command not found`や`uv: command not found`になったら、まず確認する。

```sh
~/mc-remote-stack/tools/bootstrap-ubuntu-operator.sh --check
```

`--link`を実行するよう表示されたら、linkだけを張り直す。aptやDockerには触れない。

```sh
~/mc-remote-stack/tools/bootstrap-ubuntu-operator.sh --link
```

`--install`を実行するよう表示された場合は、`5. operator toolchainを構築する`へ戻る。
