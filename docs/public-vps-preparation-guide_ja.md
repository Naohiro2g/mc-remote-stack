# 公開VPSで次のセットを準備する

リリースを待つ間に、稼働中の公開ベータと別のdirectory・volumeで、次のセットの構築準備を進める手順です。
現在のセットを動かしたまま、設定生成、配布物の照合、Caddy設定の検査、Minecraftへのファイル配置まで確認できます。
ここで起動するのは検査・準備用の一時containerだけです。Minecraftのゲームサーバーと新しい公開入口の起動は、後の切替で行います。

releaseの収集は[release artifact／preset準備手順](release-preset-preparation-guide_ja.md)、同じvolumeのまま更新する場合は[既存VPS更新手順](public-vps-bootstrap-guide_ja.md)を使います。このページは新セットを別に構築する場合の、切替前の準備を扱います。

## 1. 今回の条件を残す

利用者の短い依頼と会話から、次を一組にします。host名やbetaという名称だけでは決まりません。

| 確認すること | 今回の選択と理由に残す内容 |
| --- | --- |
| 対象 | host、稼働中project、次セットのproject、使用するStack checkout |
| 構築方法 | 既存セットを更新するか、別のセットを作って切り替えるか |
| 公開範囲 | 一般公開、LAN内、private networkのどれを使い、各端末がどう接続するか |
| release | 正式配布物が揃ったrelease。公開前の準備では、代わりに使う公開済みreleaseと、その用途 |
| 保存データ | world、権限DB、plugin設定、McRemoteの認証データ、TLS関連データをどう引き継ぐか |
| plugin | 権限管理・統合版・client互換性に使うJARと、今回変更する範囲 |
| 停止 | 停止を許容する時間、切替する接続先、再開するセット |

公式運用の実値はBackstageへ、OSS利用者は自分のprivate opsリポへ記録します。公開側には共通手順を残します。
既に会話や管理情報で決まっている条件を使い、未確認の点や今回変わる点を対話で補います。

次セットのデータ領域を分けても、引き継ぐworldの名前は維持します。公開VPSのrendererは`world.identity`をMinecraftの`LEVEL`へ使うため、名前を変えると別のworldを選びます。

## 2. 対象hostを読み取り確認する

管理情報を読み、対象host上で次を確認します。

- Stack commit、checkoutの変更有無、Python／uv／Docker／Composeと管理ユーザーの操作権限
- 稼働中セットのorder／lock、container、volume、実際のmountとTCP／UDP listener
- 公開URLと認証、`mcrctl operator check`、`mcrctl doctor`の結果
- 空きメモリ、空きディスク、コピーするデータ量とbackupの状態

Javaと統合版は別の通信です。このVPS構成は、JavaにTCP、Geyserに同じ番号のUDPを使います。WebのHTTPS転送だけでは統合版の接続確認になりません。

資源があっても、同じIPとportには二つのセットを同時に起動できません。ここでは次セットのファイル配置まで進め、現在の公開入口とゲームportは稼働中セットが使い続けます。

## 3. 別のStack checkoutとprojectを用意する

以下のcommandは対象VPS上で実行します。`SOURCE_PROJECT`、`NEXT_PROJECT`、`STACK_REVIEW`を、今回決めたpathへ置き換えます。

```sh
SOURCE_PROJECT="$HOME/mc-remote-deployments/<稼働中セット>"
NEXT_PROJECT="$HOME/mc-remote-deployments/<次のセット>"
STACK_REVIEW="$HOME/mc-remote-stack-preparation"
STACK_COMMIT="<review済みStackコミット>"

git -C "$HOME/mc-remote-stack" fetch origin "$STACK_COMMIT"
git -C "$HOME/mc-remote-stack" worktree add --detach \
  "$STACK_REVIEW" "$STACK_COMMIT"
cd "$STACK_REVIEW"
uv sync --locked
MCRCTL="$STACK_REVIEW/.venv/bin/mcrctl"

install -d -m 700 "$NEXT_PROJECT"
cp "$SOURCE_PROJECT/mc-remote.toml" "$NEXT_PROJECT/mc-remote.toml"
cp -a "$SOURCE_PROJECT/operator" "$NEXT_PROJECT/operator"
```

次セットのorderとoperator入力を編集します。稼働中projectのlockや生成物はコピーせず、次セットの入力から生成します。

| 項目 | 次セットへの設定 |
| --- | --- |
| `deployment.name`／`environment.identity` | 次セットの名前 |
| `runtime.volumes[].identity` | 各roleに新しいvolume名 |
| `world.identity` | worldを引き継ぐ場合は元と同じ名前 |
| profile／preset | 対応する公開VPS構成と、今回照合するrelease |
| public routes／port | 切替後に利用する入口。現在と同じ入口なら停止後に使う |
| Minecraft設定／connection targets／notice | 今回の利用条件に合う内容 |
| plugin入力 | 引き継ぐJARのfilename、origin、SHA-256。周辺pluginの更新は必要なものを選ぶ |
| backup入力 | 次セット専用の、存在するhost directory |

旧セットのデータを保持することと、新しいvolumeに引き継ぐことは別です。コピーする対象をここで記録し、権限DBや認証データが未反映なら、その状態も残します。

## 4. 設定と配布物を確認する

```sh
"$MCRCTL" operator check --project "$NEXT_PROJECT"
"$MCRCTL" validate --project "$NEXT_PROJECT"
"$MCRCTL" resolve --project "$NEXT_PROJECT"
"$MCRCTL" artifact fetch --project "$NEXT_PROJECT"
"$MCRCTL" render --project "$NEXT_PROJECT"
"$MCRCTL" plan --project "$NEXT_PROJECT"
```

lockのrelease、volume名、world名、public routes、plugin一覧を確認します。生成されたComposeも、次セットのproject・新volume・所定のmountになっていることを確認します。

公開前のreleaseを待つ場合は、公開済みreleaseでこの経路を確認できます。そのlockを待っているreleaseの配備結果とは扱わず、準備用に使ったreleaseを明記します。正式配布物が揃った後、新しいimmutable presetを作り、次セットのorderからlockと生成物を作り直します。

## 5. 次セットのvolumeを用意する

この段階でMinecraft data volumeを作る場合は、`mcrctl plan`の名前とlockを使います。新しいvolumeかを確認してから、三つのroleそれぞれについて作成します。

```sh
NEXT_DEPLOYMENT="<orderのdeployment.name>"
NEXT_ENVIRONMENT="<orderのenvironment.identity>"
WORLD_IDENTITY="<orderのworld.identity>"
NEXT_LOCK_ID="sha256:<resolveが返したlock ID>"
NEXT_VOLUME="<planに表示されたvolume名を一つ>"

docker volume inspect "$NEXT_VOLUME"

docker volume create --driver local \
  --label io.mc-remote.owner=mcrctl \
  --label io.mc-remote.deployment="$NEXT_DEPLOYMENT" \
  --label io.mc-remote.environment="$NEXT_ENVIRONMENT" \
  --label io.mc-remote.world="$WORLD_IDENTITY" \
  --label io.mc-remote.created-by-lock="$NEXT_LOCK_ID" \
  "$NEXT_VOLUME"
```

`inspect`で存在しないことを確認した名前を使います。既に存在する場合は、保存内容と所有者を確認して再開します。labelは後のapplyが、対象セットの保存領域であることを確認するための情報です。

## 6. 一時containerで構築を確認する

`NEXT_DEPLOYMENT`を次セットの名前にします。固定imageは事前にpullしておきます。

```sh
docker compose --project-name "$NEXT_DEPLOYMENT" \
  --project-directory "$NEXT_PROJECT/generated" \
  -f "$NEXT_PROJECT/generated/compose.yaml" pull --quiet

docker compose --project-name "$NEXT_DEPLOYMENT" \
  --project-directory "$NEXT_PROJECT/generated" \
  -f "$NEXT_PROJECT/generated/compose.yaml" config --quiet

docker compose --project-name "$NEXT_DEPLOYMENT" \
  --project-directory "$NEXT_PROJECT/generated" \
  -f "$NEXT_PROJECT/generated/compose.yaml" \
  run --rm --no-deps --entrypoint caddy caddy \
  validate --config /etc/caddy/Caddyfile

docker compose --project-name "$NEXT_DEPLOYMENT" \
  --project-directory "$NEXT_PROJECT/generated" \
  -f "$NEXT_PROJECT/generated/compose.yaml" \
  run --rm --no-deps -e SETUP_ONLY=true minecraft
```

Composeの`run`は、このcommandではserviceの公開portを割り当てません。Caddyは設定を検査し、Minecraft imageはPaper・plugin・設定を配置して、ゲームサーバーの起動前に終了します。`SETUP_ONLY`の意味は[imageの公式説明](https://docker-minecraft-server.readthedocs.io/en/latest/configuration/misc-options/)も参照できます。

Minecraft data volume内の`/data/plugins/*.jar`のSHA-256を、lockとplugin入力へ照合します。LuckPerms、Geyser、Floodgate、ViaVersion、ViaBackwardsが揃うことも確認します。これはファイル配置の確認であり、pluginの起動や権限判定の確認は初回起動後に行います。

最後に、一時containerが終了・削除されていることと、稼働中セットのdoctorが引き続き正常であることを確認します。

## 7. 切替へ渡す情報

準備結果には、Stack commit、準備に使ったrelease／lock、次セットのpath・volume、Caddy検査、JAR照合、現行セットの稼働確認を残します。

残る作業は、正式releaseの収集・反映、停止中のデータコピー、公開入口の切替、初回起動、doctorと端末からの接続確認です。コピー時はworldだけでなく、今回引き継ぐ権限DB・plugin設定・認証backend・TLS関連データも対象にします。稼働中の書き込みとコピーが混ざらないよう、停止して内容を揃える段階を設けます。

旧セットへ戻す場合は、旧設定と旧データを組にして起動する計画を残します。pluginのupgradeで設定が書き換わる場合、JARだけを戻しても元の動作に戻るとは限りません。

切替の実施結果は、この準備の結果とは別に確認します。再開時に会話履歴を必要としないよう、現在の段階、確認済みの範囲、残るデータコピー、次に使うcommandと対象をprivate opsへ渡します。
