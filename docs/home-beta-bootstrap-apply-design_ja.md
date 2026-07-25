# `home-beta` bootstrap apply 設計

## 0. 文書の位置づけ

この文書は、新TOML stackから最初のlive environmentを起動するbootstrap applyの詳細設計SSOTである。
既存runtimeのupgradeや任意Compose projectの実行手順ではない。

- 状態: 実装済み（初回bootstrap slice）
- knowledge参照commit:
  `f1b99a049b6bc57799c3356c3e54d29e45031451`
- 主な根拠:
  `2026-07-06-03`、`2026-07-21-03`、`2026-07-23-01`〜`2026-07-23-04`
- 対象:
  current lockとrenderのbinding、local Docker preflight、初回volume作成、Compose起動、
  container rollback、live evidence境界
- 対象外:
  既存world import、upgrade、Minecraft downgrade、複数projectのhost transaction、
  firewall変更、Docker導入、backup / restore、protocol互換性のratify

公開可能なapply機構とrunbookはこのrepoが所有する。host名、IP、provider、inventory等の実値は
`mc-remote-backstage`、token・password・private key・秘密を含むraw logはGit外を正とする。

## 1. 初回sliceの決定

初回applyは次のexact contractだけを受理する。

| contract | profile | preset | channel | exposure | purpose |
| --- | --- | --- | --- | --- | --- |
| private beta | `home-server@2` | `mcremote-paper@1` | `beta` | `isolated` | `integration` |
| private alpha | `home-server@2` | `mcremote-paper@2` | `alpha` | `isolated` | `integration` |

どちらもrendererは`compose@1`、serviceは`minecraft`、runtime volume roleは`minecraft-data`とする。

environment identityはorderの明示値を使い、`home-beta`という文字列からaxisを推測しない。
この制限は最初のlive integration面を狭く保つbootstrap guardであり、profile一般の恒久制約ではない。

`official-vps` legacy fixture、`lan-only` / `public`、既存worldをこのbootstrap applyへ入れない。
private alphaはbetaと別project / volume / world / portでだけ許可する。

## 2. CLI contract

```text
mcrctl apply \
  --project <exact-project-root> \
  --output <managed-render-root> \
  --expected-lock-identity sha256:<64-hex> \
  --docker-context <explicit-local-context> \
  --bootstrap \
  --yes \
  [--allow-unverified] \
  [--allow-eol] \
  [--wait-timeout 300]
```

- `--expected-lock-identity`は直前に人間がreviewした`mcrctl plan`の値とする。
- `--docker-context`はambient contextを使わないため必須とする。
- 初版は対象host上の`unix://` local Docker endpointだけを許可する。TCP / SSH contextを拒否する。
- `--bootstrap`はupgradeでなく初回起動であること、`--yes`はhost mutationのone-shot確認である。
- unverified / EOLはorder内の理由付きacknowledgementに加え、apply時にも対応するone-shot flagを要求する。
- `wait-timeout`は30〜1800秒とし、既定300秒とする。

Docker context名はprojectやlockへ保存しない。物理hostはprivate inventoryであり、
同じ公開project mechanismを各operatorが対象host上で実行する。

## 3. mutation前の入力検証

Docker daemonへ接続する前に、次をすべて検証する。

1. exact project rootのorderがvalidである。
2. lockが存在し、order・bundled profile / preset・adapter入力に対して`unchanged`である。
3. lock本文から再計算したidentityが記録値と一致する。
4. CLIの`expected-lock-identity`がcurrent lockと一致する。
5. generated treeがmanaged manifestを持ち、file digestが一致する。
6. manifestのlock identity / render-plan digestがcurrent lockと一致する。
7. current lockとartifact storeからcanonical `compose@1` bytesを再生成し、generated treeとbyte一致する。
8. HTTPS file artifactをcontent-addressed storeから再hashし、lock digestと一致させる。
9. §1のbootstrap contract、EULA、unverified / EOL gateを満たす。

manifestのdigestだけを書き換えたself-consistentな改変も、canonical renderとのbyte比較で拒否する。
applyはselectorを再解決せず、artifactをURLから取得せず、render outputを修正しない。

## 4. host preflight

§3の後、次をread-only順で実行する。

1. 指定Docker contextをinspectし、endpointがlocal Unix socketであることを確認する。
2. Docker daemonとCompose v2を検出する。
3. [`docker compose config --quiet`](https://docs.docker.com/reference/cli/docker/compose/config/)で
   exact compose fileをvalidationする。
4. Compose project labelで既存containerを列挙する。
5. exact volume nameを列挙する。
6. 既存volumeがあれば、local driverと全mcrctl ownership labelを検証する。
7. 既存containerがあれば、project / service / deployment / environment / world / lock labelと
   running stateを検証する。
8. 新規起動時はDocker publish一覧とhost socket bindの両方でJava / McRemote port衝突を検査する。

未知container、複数container、未知volume、停止中container、別lock、port衝突を自動修復しない。
exact lockのmanaged containerが既にrunningなら`status=unchanged`のno-opとする。

## 5. mutation順序

preflight成功後だけ、次の順で状態を変更する。

1. [`docker compose pull --policy always`](https://docs.docker.com/reference/cli/docker/compose/pull/)で
   composeに固定されたtag + OCI digestのimageを取得する。containerはまだ起動しない。
2. volumeが無い場合だけ、local driverと次のlabelで作成する。
   - `io.mc-remote.owner`
   - `io.mc-remote.deployment`
   - `io.mc-remote.environment`
   - `io.mc-remote.world`
   - `io.mc-remote.created-by-lock`
3. volumeを再inspectする。`docker volume create`は同名volumeを再利用し得るため、create成功文字列だけを
   ownership根拠にしない。
4. [`docker compose up --detach --wait --no-build --pull never`](https://docs.docker.com/reference/cli/docker/compose/up/)
   でexact serviceを起動する。
5. project containerを再列挙し、exactly one、running、current lock labelを確認する。

既存の正しいmanaged volumeだけが残りcontainerが無い場合は、失敗後のretryとして
`status=resumed`で起動できる。別lockで作ったvolumeはbootstrap pathで再利用しない。

## 6. rollback境界

image pull失敗はvolume作成前に停止する。volume作成後のCompose起動・postcheck失敗では
`docker compose down --timeout 120`を実行し、container / Compose networkを戻す。

external `minecraft-data` volumeは削除しない。起動途中でもworld bytesが書かれ得るため、
自動削除はrollbackでなくruntime-owned stateの破壊になる。managed label付きvolumeを保持し、
同じlockで再試行できる。

rollback自体が失敗した場合は`apply_rollback_failed`で停止し、成功を主張しない。

## 7. stable diagnostics

少なくとも次を機械的に区別する。

- `bootstrap_confirmation_required`
- `apply_confirmation_required`
- `apply_lock_identity_mismatch`
- `render_output_missing`
- `render_output_not_current`
- `bootstrap_contract_unsupported`
- `docker_context_invalid`
- `docker_context_unavailable`
- `docker_context_not_local`
- `docker_unavailable`
- `docker_compose_unavailable`
- `compose_config_invalid`
- `bootstrap_runtime_unmanaged`
- `bootstrap_runtime_not_running`
- `bootstrap_volume_unmanaged`
- `host_port_in_use`
- `compose_pull_failed`
- `bootstrap_volume_create_failed`
- `compose_up_failed`
- `apply_postcheck_failed`
- `apply_rollback_failed`

Docker stderrやcontainer logを通常のCLI出力へ転記しない。秘密を含む調査logが必要ならGit外へ置く。

## 8. `home-beta` live integration

公開runbookの手順は対象host上で行う。private host値やSSH credentialをこのrepoへ追加しない。

1. target hostと個人管理者SSH sessionをprivate inventoryで確認する。
2. target hostでこのrepoの検証済みcommitをcheckoutし、`uv sync --extra dev`、test、Ruffを通す。
3. `home-beta` projectを`home-server@2` / `mcremote-paper@1` /
   `beta` / `isolated` / `integration`でinitする。
4. EULA、resolve、exact artifact fetchを完了する。初回evidence取得時だけ理由付きunverified
   acknowledgementを使用した。
5. `plan`をreviewし、lock identity、bind port、volume、worldを記録する。
6. managed renderを生成する。
7. §2のapplyを実行する。
8. `status=created`または同じlockの`unchanged`を確認する。
9. `mcrctl doctor --project <project>`でcanonical render、managed runtime、port、token無しhelloを
   `live-auto`確認する。
10. pairingや実player操作は`live-human`として分ける。

apply成功だけではprotocol compatibilityのverified主張にならない。初回live smokeのsanitized
evidenceを別管理し、現在はexact subject `home-server@2` + `mcremote-paper@1`を束縛する
compatibility record `home-server-2-mcremote-paper-1-live-auto`が追加済みである。

### 8.1 read-only doctor境界

`doctor`は既定で`<project>/generated`とlocal Docker context `default`を確認する。applyと違い
lock identityの再入力、`--yes`、unverifiedのone-shot許可を要求せず、host状態を変更しない。

- current lockとcanonical renderをDocker接続前に検証する。
- local Unix Docker context、daemon、Compose configをread-only確認する。
- exactly oneのcurrent container、managed volume、running / healthy、exact port publishを確認する。
- lockの`mcremote-plugin.protocol`、`paper-server.minecraft_version`、world identityを使って、
  TCPへLF終端のtoken無しJSON-RPC `hello`を1回だけ送る。
- hello成功時はpublic contract fieldだけを検証し、`auth_required`はresponsiveとして区別する。
- container logやhelloの生responseを通常出力へ載せず、session / player / tokenを表示しない。

doctor PASSは現在のruntimeがlockに一致して最小helloへ応答する証拠であり、pairing、実player操作、
全command、backup、upgrade、公開networkの証拠ではない。

## 9. test-first gate

- lock / acknowledgement / bootstrap flag failure時にDockerへ接触しない
- self-consistentなgenerated改変もcanonical bytes不一致で拒否する
- remote Docker contextをdaemon接続前に拒否する
- unknown volume / port衝突をpull前に拒否する
- image pullをvolume作成より先に行う
- create後にvolume ownershipを再inspectする
- up失敗時にComposeをdownし、volumeを削除しない
- exact running containerへの二回目applyをno-opにする
- CLIがlock identity、Docker context、bootstrap、confirmation、one-shot acknowledgementを明示転送する
- doctorはgenerated drift、remote context、unmanaged / unhealthy runtime、port driftをprotocol接続前に拒否する
- doctorのhelloはTCP実LF終端とし、protocol / Minecraft / world不一致を拒否する
- doctorはDocker変更commandを呼ばず、生responseやcredential fieldを出力しない

## 10. 次のslice

bootstrap後は次を別々に閉じる。

1. hello以外のread-only / reversibleな`live-auto` protocol command smoke
2. backup / restoreの実機検証
3. deployed-state履歴とupgrade rollback
4. host-level multi-project collision transaction
5. `lan-only` exposureとfirewall責任分界
6. `home-alpha`の別volume・別world追加

Minecraft versionを下げて同じworldを再利用するdowngradeや汎用`--force`は追加しない。
