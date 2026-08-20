# Fresh host bootstrap guide

fresh な Ubuntu 系 host を、安全に `mc-remote-stack` の検証・生成を始められる地点まで運ぶ。
provider、実 IP、個人名、秘密値には依存しない。

クリーンインストール直後だけを利用条件にはしない。既存Ubuntu hostでは、既に入っているtoolや
serviceをpreflightで観測し、要件を満たすものはそのまま使う。無関係なpackageやdesktop環境を
一括削除して「fresh」に寄せない。

実利用者の初期構築も、`mcrctl` install前からagent支援を利用できる。ただし、対象host上への
agent installは必須にせず、人間のterminalだけで完走できる手順を基準にする。管理端末からの
SSH支援、対象host上agentを試す場合のsecurity gate、人間が握るcheckpointは
[`agent-assisted bootstrap guide`](agent-assisted-bootstrap-guide_ja.md)を正とする。

## 現在の実装境界

現行の vertical slice は deployment project の `init`、`validate`、`repo check`、`plan`、
EULA gate、`resolve`、`artifact fetch`、`render`に加え、isolated `home-beta`と
Caddy / Scratch / Bridge / Minecraft / McRemoteを含む`vps-server@6` / `public-web-paper@2`
public betaの
初回bootstrap applyまでを
実装している。public VPSの手順は
[`public VPS bootstrap guide`](public-vps-bootstrap-guide_ja.md)を正とする。既存world import、
upgrade、複数project transaction、firewall変更、外部HTTPS / WSS readiness claimは
実装していない。この文書も **production readinessやprotocol compatibilityをbootstrap
applyだけから主張しない**。

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
Ubuntuではcloud-initが`50-cloud-init.conf`でpassword loginを先に有効化している場合がある。
OpenSSHは各keywordについて最初に得た値を使うため、それより前に評価される`00-`のdrop-inを使う。
既存drop-inを削除・編集して解決しない。

```bash
sudo install -d -m 755 /etc/ssh/sshd_config.d
printf '%s\n' \
  'PermitRootLogin no' \
  'PasswordAuthentication no' \
  | sudo tee /etc/ssh/sshd_config.d/00-mc-remote-bootstrap.conf
sudo sshd -t
sudo systemctl reload ssh
sudo sshd -T | awk '
$1=="permitrootlogin" ||
$1=="passwordauthentication" ||
$1=="pubkeyauthentication" {
  print
}'
```

実効値が`permitrootlogin no`、`passwordauthentication no`、`pubkeyauthentication yes`になった
ことを確認する。reload 後も別 terminal で SSH と `sudo -v` を再確認する。失敗した sessionを
唯一の管理経路にしない。

## 3. 運用者環境を構築する

足りないcommandごとに代替手順へ分岐しない。このrepoのUbuntu用bootstrapを、運用者環境の
唯一の入口にする。最初はread-only checkを実行する。

```bash
git clone https://github.com/Naohiro2g/mc-remote-stack.git
cd mc-remote-stack
tools/bootstrap-ubuntu-operator.sh --check
```

不足が報告された場合は、同じscriptへ明示的なinstall権限を与える。

```bash
tools/bootstrap-ubuntu-operator.sh --install
```

scriptはUbuntuのsupport対象versionだけを受理し、`ca-certificates`、`curl`、`git`、固定versionの
`uv`、Python 3.11、Docker Engine、Compose 2.33.1以上を準備する。Dockerが未導入の場合は
Docker公式APT repositoryを使う。既存Dockerを未知の配布元から暗黙置換しない。

この個人管理者はdeploymentを行う**信頼された運用者**であり、既にsudo管理者である。毎回
`mcrctl`全体をsudo経由で実行する代わりに、明示的な承認のもとで`docker` groupへ追加する。Docker公式文書が
警告する通り、このgroupはroot相当の権限を持つ。agent専用userや一般利用者を追加してはならない。
group追加後はlogout/loginし、`--check`を再実行する。`mcrctl`のroot実行は使用せず、CLIも拒否する。

過去の`mcrctl` root実行で一つのprojectがroot所有になった場合だけ、対象をexact pathで限定して修復する。

```bash
tools/bootstrap-ubuntu-operator.sh --install \
  --repair-project "$HOME/mc-remote-deployments/<deployment>"
```

`--repair-project`は`$HOME/mc-remote-deployments`直下のTOML projectだけを受理する。home directoryや
deployment root全体を再帰変更しない。

## 4. checkout と運用者環境の自己検証

bootstrapはcheckout内の`.venv`も構築する。loginし直した後、次を一巡する。

```bash
tools/bootstrap-ubuntu-operator.sh --check
uv run pytest
uv run ruff check .
uv run mcrctl --help
```

ここで失敗したら deployment project を作らず、toolchain または repository の状態を直す。
bootstrap期の`uv run mcrctl`は、このcheckoutの`.venv`を使うrepo-boundな呼び方であり、
`mcrctl`をuserの`PATH`へinstallしない。別のterminalでrepo外から呼ぶ場合は、checkout位置を
確認して`~/mc-remote-stack/.venv/bin/mcrctl`のようにexact pathを使う。恒久的なoperator向け
tool installを、場当たり的なsymlinkで代用しない。

## 5. isolated `home-beta` project

```bash
MC_REMOTE_PROJECT="$HOME/mc-remote-deployments/home-beta"
uv run mcrctl init "$MC_REMOTE_PROJECT" \
  --format toml \
  --deployment-name home \
  --profile home-server@4 \
  --environment-identity home-beta \
  --channel beta \
  --exposure isolated \
  --purpose integration \
  --preset mcremote-paper@1 \
  --artifact-store "$HOME/.local/share/mc-remote/artifacts" \
  --volume minecraft-data=home-beta-minecraft-data \
  --world-identity home-beta-world \
  --bind-address 127.0.0.1 \
  --java-port 25565 \
  --mcremote-port 25575
uv run mcrctl validate --project "$MC_REMOTE_PROJECT"
uv run mcrctl repo check --project "$MC_REMOTE_PROJECT"
uv run mcrctl accept-eula --project "$MC_REMOTE_PROJECT" --yes
uv run mcrctl operator check --project "$MC_REMOTE_PROJECT"
```

directory名からaxisやidentityを推測しない。`home-alpha`は同じfileへ追加せず、後から別project、
別volume、別worldとして作る。deployment projectはpackage source checkoutの外へ置き、source codeと
instance固有order / lockのowner境界を混ぜない。親directory名もenvironment identityの正本ではない。

新しいTOML projectのrootは最大`0750`、初期order / README / `.gitignore`は最大`0640`で作り、
呼出し元のumaskがそれより厳しければ緩めない。order / lockは秘密保存先ではないが、後でDocker権限で
実行するtrusted inputなので、非管理主体から書込み可能にしない。旧版で作ったprojectは自動変更しない。
所有者とgroupを確認してから、必要なprojectだけを人間が明示的に締める。

```bash
stat -c '%U %G %a %n' \
  "$HOME/mc-remote-deployments" \
  "$MC_REMOTE_PROJECT" \
  "$MC_REMOTE_PROJECT/mc-remote.toml"
chmod 750 "$HOME/mc-remote-deployments" "$MC_REMOTE_PROJECT"
chmod 640 \
  "$MC_REMOTE_PROJECT/.gitignore" \
  "$MC_REMOTE_PROJECT/README.md" \
  "$MC_REMOTE_PROJECT/mc-remote.toml"
if [ -f "$MC_REMOTE_PROJECT/mc-remote.lock.toml" ]; then
  chmod 640 "$MC_REMOTE_PROJECT/mc-remote.lock.toml"
fi
```

共有groupで複数管理者に書込みを与える運用は、この個人管理者baselineへ暗黙追加しない。
artifact storeの公開artifact bytesは`0755/0644`、local secret storeは`0700/0600`の別境界とする。

exact subject `home-server@4` + `mcremote-paper@1`は、認証強制込みのlive evidenceが着地するまで
unverifiedである。理由付きacknowledgementをorderへ記録し、生成前にplanをreviewする。

```bash
uv run mcrctl resolve --project "$MC_REMOTE_PROJECT" --allow-unverified
uv run mcrctl plan --project "$MC_REMOTE_PROJECT"
uv run mcrctl artifact fetch --project "$MC_REMOTE_PROJECT"
uv run mcrctl render \
  --project "$MC_REMOTE_PROJECT" \
  --output "$MC_REMOTE_PROJECT/generated"
```

このexact subjectの`plan`は`compatibility=verified`を表示してstatus 0を返す。lock identity、
profile / preset digest、artifact、bind port、volume、worldを人間が確認する。秘密値はdeployment
projectに保存しない。profile、preset、component setが異なるsubjectは、別のevidenceでcoverage
されない限りunverified gateの対象である。

## 6. bootstrap apply

reviewしたlock identityを手入力し、同じtarget host上の明示local Docker contextへ適用する。

```bash
REVIEWED_LOCK_IDENTITY="sha256:<planで確認した64-hex>"
uv run mcrctl apply \
  --project "$MC_REMOTE_PROJECT" \
  --output "$MC_REMOTE_PROJECT/generated" \
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

## 7. ログイン後のread-only稼働確認

状態確認に`apply`を再利用しない。対象hostの個人管理者terminalで次を実行する。

```bash
~/mc-remote-stack/.venv/bin/mcrctl doctor \
  --project ~/mc-remote-deployments/home-beta
```

上のpathはこのguideどおりhome directory直下へclone / initした場合である。既定では
`<project>/generated`とlocal Docker context `default`を使う。別のmanaged
renderやlocal contextを意図して確認する場合だけ`--output` / `--docker-context`を明示する。

doctorは次をread-onlyで確認する。

- order / lock / bundled profile・presetとcanonical generated treeがcurrent
- Docker contextが対象host上のlocal Unix socket
- managed volumeのidentity / ownership labelがcurrent lockと一致
- exactly oneのmanaged containerがcurrent lockと一致し、runningかつhealthy
- Java / McRemote portがlockどおりにpublishされ、isolated profileではloopback限定
- lock済みendpointがtoken無しJSON-RPC helloを`auth_required`で拒否すること

token無しhelloが成功した場合は`doctor_auth_not_enforced`でFAILする。
doctorはcontainer log、生response、session / player / tokenを出力しない。低レベル状態だけを人間が
学習・切り分けしたい場合は次も使えるが、lock / protocolとの一致までは主張しない。

```bash
cd ~/mc-remote-deployments/home-beta/generated
docker compose ps
```

## 8. 現在の停止点

doctorのhello PASSまででcontainer-level bootstrapと最小`live-auto`は確認できる。次が閉じる前に、
exact `home-server@4` + `mcremote-paper@1`は認証強制込みの再検証が必要であり、このbootstrapを
一般production、公開network、認証全体、upgrade可能と読み替えない。

- hello以外のprotocol command smoke
- backup / restore の実機検証
- auth-enforcement以外の汎用upgrade / rollback deployed-state transaction
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
