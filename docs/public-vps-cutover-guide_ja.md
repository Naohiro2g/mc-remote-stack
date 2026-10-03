# 別に構築したVPSセットへ切り替える

[構築準備](public-vps-preparation-guide_ja.md)で設定と配布物を確認した次セットへ、停止時点のデータを引き継ぎ、同じ公開入口で起動する手順です。以下は対象VPS上で実行します。各段階の結果を確認して進み、失敗時はその段階と理由を記録します。

## 1. 切替条件を揃える

準備記録から次を確認します。

- 正式releaseのexact preset、Stack commit、次projectのlock、生成物の一致
- 元と次のproject名・path・各volumeの実名と所有label、実際のmount
- 引き継ぐworld名、権限DB、plugin設定、McRemote認証backend、TLSデータ
- 今回の停止時間、バックアップ先、再開するセット、公開URLとTCP／UDP port
- サービス再開後の確認項目と、運用者noticeの採用文面

運用者noticeは一時ファイルで内容を確認・編集し、未稼働の次projectの入力へ収容してから生成します。旧セットからコピーした案内も、今回のreleaseと用途に合う内容へ編集します。Scratchイメージに含まれる製品noticeは、そのreleaseの配布内容を確認します。

```sh
SOURCE_PROJECT="$HOME/mc-remote-deployments/<旧セット>"
NEXT_PROJECT="$HOME/mc-remote-deployments/<次セット>"
SOURCE_DEPLOYMENT="<旧Compose project名>"
NEXT_DEPLOYMENT="<次Compose project名>"
MCRCTL="$HOME/mc-remote-stack-preparation/.venv/bin/mcrctl"
NEXT_LOCK_ID="sha256:<今回のlock ID>"
COPY_IMAGE="<lockのMinecraft image locator>@sha256:<digest>"
BACKUP_DIR="$HOME/mc-remote-backups/<今回の切替記録>"

"$MCRCTL" operator check --project "$NEXT_PROJECT"
"$MCRCTL" validate --project "$NEXT_PROJECT"
ss -lntu
sysctl net.ipv4.ip_unprivileged_port_start
```

一般ユーザーによるbind検査とDocker daemonによるport公開は権限が異なります。1024未満のportを一般ユーザーがbindできないhostでは、bootstrap applyのport検査は権限不足で止まります。使用中という意味ではありません。公開portと起動方法を停止前に確認します。

新しいCompose networkでは、hostのDocker用フィルタが以前のnetwork名・subnet・宛先だけを通す設定になっていないかも確認します。UFWやproviderのport許可、containerのlistenerだけで外部到達は確認できません。

## 2. 旧セットを停止する

```sh
docker compose --project-name "$SOURCE_DEPLOYMENT" \
  --project-directory "$SOURCE_PROJECT/generated" \
  -f "$SOURCE_PROJECT/generated/compose.yaml" down --timeout 120
docker ps --filter "label=com.docker.compose.project=$SOURCE_DEPLOYMENT"
```

world等の書き込みが止まってからコピーします。このcommandは旧volumeを保持します。停止時刻を記録し、旧volumeを使う別containerも稼働していないことを確認します。

## 3. 停止時点を保存し、各volumeをコピーする

旧projectの入力・生成物と旧volumeを組にして保存します。world、認証、TLSを含むバックアップはprivateな保存先で管理します。

```sh
install -d -m 700 "$BACKUP_DIR"
cp -a "$SOURCE_PROJECT" "$BACKUP_DIR/source-project"
```

次をMinecraft data、Caddy data、Caddy configの各roleについて実行します。`SOURCE_VOLUME`は実際のmountで確認した旧volume名、`NEXT_VOLUME`は準備した次volume名です。次volumeの内容も確認し、保持すべきデータが入っていれば先に保存します。

```sh
VOLUME_ROLE="<今回コピーするrole>"
SOURCE_VOLUME="<そのroleの旧volume実名>"
NEXT_VOLUME="<そのroleの次volume実名>"
docker volume inspect "$SOURCE_VOLUME" "$NEXT_VOLUME"
docker ps --filter "volume=$SOURCE_VOLUME"
docker ps --filter "volume=$NEXT_VOLUME"

docker run --rm --network none --user 0:0 \
  --mount "type=volume,source=$SOURCE_VOLUME,target=/source,readonly" \
  --mount "type=volume,source=$NEXT_VOLUME,target=/target" \
  --mount "type=bind,source=$BACKUP_DIR,target=/backup" \
  --env "VOLUME_ROLE=$VOLUME_ROLE" \
  --env "OPERATOR_UID=$(id -u)" --env "OPERATOR_GID=$(id -g)" \
  --entrypoint bash "$COPY_IMAGE" -euc '
tar -C /source -cpf "/backup/$VOLUME_ROLE.tar" .
chmod 600 "/backup/$VOLUME_ROLE.tar"
chown "$OPERATOR_UID:$OPERATOR_GID" "/backup/$VOLUME_ROLE.tar"
cp -a /source/. /target/
diff -qr /source /target
sha256sum "/backup/$VOLUME_ROLE.tar"
'
```

worldの全dimension、LuckPerms DB、周辺pluginの設定と秘密鍵、McRemoteのsnapshotとauthorityを揃えて引き継ぎます。認証backendが健全な既存セットでは、引き継ぎのために再初期化する操作は不要です。全ファイルの内容一致とバックアップの読取りを確認し、roleごとにコピー元・先・結果を記録します。

## 4. 初期設定の同期と持込設定を照合する

生成したMcRemote設定は初回配置用のseedです。現行imageは`SYNC_SKIP_NEWER_IN_DESTINATION=true`で同期するため、持ち込んだ設定の更新日時がseedより古いとseedで上書きされます。旧設定を採用する場合は、コピーした次volume側の設定だけをseedより新しい更新日時にし、変更前後の内容hashを照合します。

```sh
NEXT_MINECRAFT_VOLUME="<次Minecraft data volume>"
docker run --rm --network none --user 0:0 \
  --mount "type=volume,source=$NEXT_MINECRAFT_VOLUME,target=/data" \
  --mount "type=bind,source=$NEXT_PROJECT/generated/minecraft,target=/config,readonly" \
  --entrypoint bash "$COPY_IMAGE" -euc '
stat -c "%Y %n" /config/plugins/McRemote/config.yml /data/plugins/McRemote/config.yml
sha256sum /data/plugins/McRemote/config.yml
touch /data/plugins/McRemote/config.yml
test /data/plugins/McRemote/config.yml -nt /config/plugins/McRemote/config.yml
sha256sum /data/plugins/McRemote/config.yml
'
```

これは内容を採用する判断とは別の、同期時の保持確認です。server.propertiesと周辺plugin設定も今回の入力・実値へ照合します。b8起動時にはMcRemoteの旧config keyが移行されるため、旧設定は旧データとともに保存しておきます。

## 5. 次セットを起動する

一般ユーザーのport検査が可能なhostでは、準備済みのlockと生成物でbootstrap applyを実行します。

```sh
"$MCRCTL" apply --project "$NEXT_PROJECT" \
  --output "$NEXT_PROJECT/generated" --docker-context default \
  --bootstrap --yes --expected-lock-identity "$NEXT_LOCK_ID"
```

port検査が権限不足になるhostでは、空きport・Docker用フィルタ・volume ownership・設定・image取得を確認した後、次のCompose commandで起動できます。Docker自身も公開portの衝突を検査します。`host_port_probe_permission_denied`はその権限不足を示します。以前のStackでは同じ失敗が`host_port_in_use`と表示されるため、DETAILの`Permission denied`も確認します。

```sh
docker compose --project-name "$NEXT_DEPLOYMENT" \
  --project-directory "$NEXT_PROJECT/generated" \
  -f "$NEXT_PROJECT/generated/compose.yaml" \
  up --detach --wait --wait-timeout 300 --no-build --pull never
"$MCRCTL" doctor --project "$NEXT_PROJECT"
```

失敗時は、起動したcontainerと未起動service、失敗理由を確認します。旧セットを再開する場合は次セットを停止し、保存した旧設定・旧volumeを組にして起動します。次セット再開後のworld更新、権限変更、credentialの発行・失効があれば、その変更をどう扱うかも確認します。

## 6. 再開後の動作と運用を確認する

- doctorのruntime、lock／render、protocol／auth、homepage、Scratch runtime、WireScope
- 正式McRemote JARと周辺JARのSHA、旧JARの有無、LuckPerms使用、credential backendの健全性
- world名とdimension、creative等の採用設定、引き継いだ権限・認証データ
- 公開HTTPS、認証付きScratchのペアリング／hello／chat、WireScopeでの応答、notice表示
- JavaのTCPと統合版のUDPを別々に、VPS外の端末から確認
- 新しいbackup保存先と、既存の暗号化・転送・予約実行の参照先

UDP status応答とゲームへの参加、配信JSONの一致と画面表示はそれぞれ別の確認です。未確認の項目も記録します。通常の`mcrctl`が使うStack checkoutを今回のpresetへ対応させ、次projectでoperator checkとdoctorを再確認します。

private opsには、稼働中project・新旧volumeの状態、停止／再開時刻、Stack commit、採用入力、確認結果、残る端末チェックと次のcommandを残します。OSS利用者も自身のprivate opsで同じ情報を管理できます。
