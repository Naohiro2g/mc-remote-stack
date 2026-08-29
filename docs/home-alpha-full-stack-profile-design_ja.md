# home private alpha フルスタック化設計（草案）

## 0. 位置づけと状態

- 状態: **`home-server@6`/`lan-routes@1`/`compose@14`の機構と`home-alpha-full@1`
  presetは実装・登録・test-first済み**（2026-08-29）。synthetic fixtureでの
  render検証に加え、実データ（実profile・実preset・実artifact digest）を使った
  `mcrctl init/resolve/plan/artifact fetch/render`のdry runでも動作確認済み
  （§6）。**m720s1実機へのlive apply（§7手順4以降）はagentの実行境界外**
  （SSH/Tailscaleアクセスを持たない）であり、人間が行う。§3〜§4は当初案から
  Tailscale証明書の実際の制約に合わせて改訂している（下記参照）。
- **設計変更の経緯**: 当初案（below §3/§4の旧稿）はCaddyコンテナをTailscale
  interface（CGNAT `100.64.0.0/10`）へ直接bindし、`tailscale cert`で得た証明書を
  Caddyへmountする方式だった。実装時に`toml_project.py`の既存cross-field
  validationが`isolated`露出は**loopback**、`lan-only`露出は**RFC 1918**の
  bind_addressしか許可せず、Tailscale CGNATアドレスはどちらにも該当しないと判明した
  （この検証はhome-server全profile共通の既存契約であり、本設計のために変更していない）。
  そのため設計を「Caddyはloopbackへbindし、host側で`tailscale serve`
  （運営者が対象host上で実行する、Tailscale標準機能）がtailnet向けTLS終端と
  loopbackへの転送を担う」方式へ変更した。結果としてCaddy自身はTLSを持たず
  （`tailscale serve`が既にTLS終端するため）、`lan-routes@1`から
  `tls_cert_path`/`tls_key_path`を削除し、SAN証明書の要否という未確定事項も解消した。
  Stackは引き続きTailscale CLIを呼び出さない（実行境界：host側operator作業）。
- 対象host: home private alpha（m720s1、`home-server`profile）。
- 背景: `docs/home-alpha-validation-guide_ja.md`が示す現行`home-server@4`は
  Minecraft単体のみで、Scratch/Bridge/Caddyを持たない。2026-07-31 NOTESで
  「home-server向けにCaddy/Scratch/Bridgeを持つisolated/lan-only対応の新profile
  revisionを設計する」という計画が立てられたが未着手のまま残っていた。本文書は
  その実行に向けた設計である。
- 対象外: VPS公開betaの変更、home private betaの変更、McRemote/Scratch/Bridge
  各repo側のCI変更。既存の各repoのCIは「任意commitをbuildしてsha-<commit>形式
  OCI tagで公開する」機構を既に持っており（`public-web-paper@8`の
  `scratch-image`/`bridge-image`が実例）、本設計はこれを再利用するだけで新設しない。

## 1. 目的（`versioning-design_ja.md` §10.6との整合）

SSOT `10-protocol/versioning-design_ja.md` §10.6は次を定める。

> `alpha`はtag前のGitHub source commitを動かすchannelであり...配備時は実験対象
> componentだけを選択時のexact source commitからbuildし...他のcomponentは既知
> artifactへ固定し、公開artifactの版を増やさない。

ここでの「実験対象component」とは、この project が開発する McRemote plugin /
Scratch client / Bridge を指す。Caddy image、itzg/minecraft-server image、
Paper server jar は third-party infra であり、これらはalphaでも既知の安定版へ
固定する（"他のcomponent"）。

したがって、目標状態は次の通りである。

| component | pin方式 | 意味 |
| --- | --- | --- |
| McRemote plugin | `git-build`（reviewed import） | 選択時点のGitHub `develop`（またはtarget branch）HEAD commitからbuild、recipe/toolchain/commitをlockへ記録 |
| Scratch client (OCI) | `oci`、`version = "sha-<commit>"` | 選択時点のScratch-editor repo HEAD commitのCI build digest |
| Bridge (OCI) | `oci`、`version = "sha-<commit>"` | 選択時点のBridge repo HEAD commitのCI build digest |
| Caddy / itzg minecraft-server / Paper jar | `oci` / `https-file`、既知安定版 | alphaでも変動させない third-party 基盤 |

更新は**自動同期ではない**。人間またはagentが「今この時点のGitHub状態を使う」と
明示的に選び、resolve→lockという不変の記録を都度作る。1回のupdateごとに新しい
preset revision（append-only）とlockを生成し、以前の状態は履歴として保持する。

## 2. 新規 profile `home-server@6`

既存`vps-server@4`のtopology（caddy / scratch / bridge / minecraft）を、
`isolated`/`lan-only`露出向けに転写する。`vps-server@4`固有のpublic-only
security control（`explicit-public-bind`、`ipv4-only-publication`、
`origin-allowlist`の公開ドメイン前提）は持ち込まない。

```toml
schema_version = 1

[profile]
name = "home-server"
revision = "6"
description = "Single-host Compose topology with Caddy/Scratch/Bridge for isolated/lan-only alpha integration"

[capabilities]
provided = [
  "compose",
  "paper",
  "persistent-world",
  "scratch-runtime",
  "websocket-bridge",
  "mcremote-auth-enforced",
]
required_component_roles = [
  "caddy-edge",
  "scratch-runtime",
  "websocket-bridge",
  "minecraft-runtime",
  "paper-server",
  "mcremote-plugin",
]

[environment]
allowed_channels = ["alpha"]
allowed_exposures = ["isolated", "lan-only"]
allowed_purposes = ["integration"]

[policy]
required_security_controls = [
  "online-mode",
  "rcon-disabled",
  "mcremote-auth-enforced",
  "compose-edge-loopback-only",
  "tailnet-reachability-via-host-serve",
]
instance_fields = [
  "deployment.name",
  "environment.identity",
  "runtime.artifact_store",
  "runtime.volumes",
  "world.identity",
  "network.bind_address",
  "network.java_port",
  "network.mcremote_port",
  "agreements.minecraft_eula",
]
override_allowlist = ["capacity.memory"]

[renderer]
name = "compose"
revision = "14"

[[operator_input_roles]]
id = "lan-routes"
adapter = "lan-routes@1"
required = true

[[operator_input_roles]]
id = "minecraft-motd"
adapter = "minecraft-motd@1"
required = false

[[services]]
id = "caddy"
role = "caddy-edge"

[[services]]
id = "scratch"
role = "scratch-runtime"

[[services]]
id = "bridge"
role = "websocket-bridge"

[[services]]
id = "minecraft"
role = "minecraft"

[[volume_roles]]
id = "minecraft-data"
kind = "world"

[[volume_roles]]
id = "caddy-data"
kind = "runtime-data"

[[volume_roles]]
id = "caddy-config"
kind = "runtime-data"
```

`allowed_channels = ["alpha"]`限定とする。betaやstableでこのtopologyを使う場合は
別途`vps-server`系または新しい公式profileを使う（既存の段の意味を混ぜない）。

credential-rollback-separated等（`home-server@3`/`@4`由来）は今回のsliceに含めない。
必要になれば`home-server@7`として別途append-onlyで追加する。1つのprofile revisionで
複数の未検証の軸を同時に変えない。

## 3. 新規 operator input `lan-routes@1`（実装済み）

Caddyはloopbackだけへbindし、TLSを持たない（§4）。`lan-routes@1`はTailscale
MagicDNS hostnameと、Scratch/Bridgeを区別するための2つのloopback port番号だけを
持つ、3キーの最小契約にした。

```toml
# operator/lan-routes/routes.toml
hostname = "m720s1.<tailnet>.ts.net"
scratch_port = 8443
bridge_port = 8444
```

- `hostname`は`public-routes`と同じDNS名検証（`_public_dns_name`）を再利用する
  （IPリテラル禁止、小文字、`secret://`/`${`禁止）。単一hostname
  （Tailscaleは1 node＝1 MagicDNS nameが基本）なので、Scratch/Bridgeを別
  subdomainではなく別portで区別する。SAN証明書の要否という論点は、Caddyが
  そもそもTLSを扱わなくなったため消滅した。
- `scratch_port`/`bridge_port`はloopback上のport番号（1–65535、相互に別値）。
  operatorが対象host上で`tailscale serve`をこれらのloopback portへ向けて設定する
  （host側手順、Stackはこれを実行・検証しない）。

## 4. 新規 renderer `compose@14`（実装済み）

`_compose_v1`系（home-serverの既存bind_address尊重パターン）をベースに、
`_compose_v2`（vps-server@4のcaddy/scratch/bridgeサービス定義）からservice定義を
移植した。

- `network.bind_address`は既存の`toml_project.py`契約どおり、`isolated`なら
  loopback、`lan-only`ならRFC 1918アドレスのみを受け付ける（本profileは`0.0.0.0`を
  拒否する独自checkを持たない——上流の`_validate_order`が既にその組合せ自体を
  作らせない）。CaddyやMinecraftの`ports`はこの`bind_address`をそのまま使う。
- Caddyの自動HTTPS（ACME）を避けるため、Caddyfileはhostnameを持たない`:port`
  形式のsite blockにし、TLSを一切扱わない。

```caddyfile
# Generated by mcrctl compose@14. Do not edit.
:8443 {
    reverse_proxy scratch:8080
}

:8444 {
    reverse_proxy bridge:8080
}
```

- tailnet向けのTLS終端は、host側の`tailscale serve`（operatorが実行、Stackの
  管轄外）が担う。これがloopback portへ転送することで、外部（tailnet参加端末）
  からは`https://{hostname}:{tailscale-serve側で選んだport}`として到達する。
  本設計はStack側のloopback port番号と`tailscale serve`が公開するtailnet側port
  番号が一致する運用を前提とする（運用手順で明記する）。
- `runtime_config`（`scratch.json`）の`bridge_url`は`wss://{hostname}:{bridge_port}`、
  `default_sandbox`は`hostname`（bare、`_compose_v2`の`routes["minecraft"]`と同じ
  慣習）。`release_identity`はScratch artifactの`version`（`sha-<commit>`）を
  そのまま使う——これは08-29に修正した表示バグの対象と同じ値なので、footer表示は
  既に「raw tagを出さない」修正後の前提で問題ない。
- `BRIDGE_ORIGIN_ALLOWLIST`は`https://{hostname}:{scratch_port}`
  （ブラウザのOriginはport込みで送られるため）。
- homepage site blockは持ち込まない（alpha home環境にhomepageは不要）。
- connection_targets（複数接続先の切替）は本sliceでは未対応。必要になれば
  append-onlyな`compose@15`で追加する。

## 5. McRemote plugin の exact commit pin（`git-build`は現時点で未使用）

`preset.schema.json`には`gitBuildArtifact`（`kind = "git-build"`、Stackは自動build
せず人間がbuildしたreviewed出力だけを取り込む）が既に定義されている。§6で実際に
登録した2026-08-29時点では、McRemote `main`のHEAD（`4e8f1ff1bd48...`）が
`v1.21.11-2300.0.0b6`タグと**完全に一致**していた（`gh api
repos/Naohiro2g/McRemote/compare/v1.21.11-2300.0.0b6...main`でahead=0/behind=0を
確認）ため、`git-build`で改めてbuildし直す必要はなく、既存の`https-file`
（tag済みGitHub Release asset、digestをGitHub APIで再確認済み）をそのまま
再利用した。`main`が次のtagより先行するcommitを持つようになった時点で、初めて
`git-build`によるexact commit pinが必要になる。

## 6. 新規preset `home-alpha-full@1`（登録済み・2026-08-29）

`src/mc_remote_stack/data/preset_registry/home-alpha-full/1/preset.toml`として
実際に登録し、`preset_catalog_policy.toml`へ`active`登録、`preset_catalog.toml`を
`build_preset_catalog()`で再生成した。全artifactは机上の値ではなく、実際に
`gh api`（McRemote/scratch-editorのcommit・release digest）と
`docker buildx imagetools inspect`（ghcr.io OCI index digest、`docker login
ghcr.io`は`gh auth token`で認証）で再検証済み。

- `scratch-image`/`bridge-image`: `scratch-editor`の`develop`HEAD
  （`5df50144da13b1a1c8c23b01f2d0138ffd17b953`）。これは`public-web-paper@8`
  （b6）に収録済みのcommitと**一致**しており（2026-08-29時点でdevelopが
  進んでいない）、同じdigestを再利用した。
- `mcremote-jar`: McRemote `main`HEAD（`4e8f1ff1bd48bfa28c465f2dc24060fbb419317f`）
  は`v1.21.11-2300.0.0b6`タグと同一commitのため、既存のGitHub Release asset
  （`https-file`、digestをGitHub Release APIで再確認）をそのまま使った（§5）。
- `caddy-image`/`minecraft-image`/`paper-jar`: third-party基盤として
  `public-web-paper@8`と**同一の**既知digestへ固定した。Docker Hubの
  `caddy:2.11.4-alpine`タグは現在別digestを指す（ベースイメージの再ビルドで
  浮動する）ことをlive確認したが、これは想定どおりでStack側のpinを追従させない
  （§10.6「他のcomponentは既知artifactへ固定」）。

**正直な記録**: 2026-08-29時点でMcRemote `main`／scratch-editor `develop`は
どちらも直近tag（b6）とcommitレベルで完全一致しており、「tag前の最新」と
「直近beta」の間に実質差分がない。したがってこの`home-alpha-full@1`は
現時点ではbeta（`public-web-paper@8`）と中身が同じである。これは設計の欠陥では
なく、「今この瞬間はまだ何も新しく積まれていない」という事実であり、
今後developer側でcommitが積まれた時点で次のrevision（`home-alpha-full@2`）が
実際に分岐する。

```toml
[preset]
name = "home-alpha-full"
revision = "1"
description = "..."

[requirements]
profile_capabilities = [
  "compose", "paper", "persistent-world",
  "scratch-runtime", "websocket-bridge", "mcremote-auth-enforced",
]
allowed_channels = ["alpha"]
required_claims = ["profile-render", "protocol-hello"]

[[components]]
id = "caddy"
role = "caddy-edge"
artifact = "caddy-image"

[[components]]
id = "scratch"
role = "scratch-runtime"
artifact = "scratch-image"

[[components]]
id = "bridge"
role = "websocket-bridge"
artifact = "bridge-image"

[[components]]
id = "minecraft-runtime"
role = "minecraft-runtime"
artifact = "minecraft-image"

[[components]]
id = "paper-server"
role = "paper-server"
artifact = "paper-jar"
minecraft_version = "1.21.11"

[[components]]
id = "mcremote-paper"
role = "mcremote-plugin"
artifact = "mcremote-jar"
```

`compatibility_status`は初回`unverified`（実際に登録した状態も`unverified`）。
alpha検証ガイド（§7）を通してlive evidenceを取得後も、これは「public beta昇格の
合格」を意味しない——§10.6どおり、価値あるsliceが確認できたら次の`bN`をtagして
初めてbeta段へ進む。

## 7. 更新手続き（都度選び直し、非自動）

`docs/home-alpha-validation-guide_ja.md`を拡張する形で、次のrunbookを追記する
（m720s1実機へのlive apply着手時に反映）。

1. McRemote（`main`）／scratch-editor（`develop`、**Bridgeも同じrepoの
   `mc-remote/bridge`配下に同居しており別repoではない**）のtarget branch HEADを
   人間が選ぶ。
2. 対応commitのartifactが既に存在するか確認する。Scratch/Bridge:
   `docker buildx imagetools inspect ghcr.io/naohiro2g/mc-remote-<scratch|bridge>:sha-<commit>`
   で`sha-<commit>` OCI tagの存在とindex digestを確認し、なければ
   `mc-remote-images.yml`（`workflow_dispatch`、CI resourceを使う実行なので
   人間の承認を得てから起票する）を手動triggerする。McRemote: tagが同じcommit
   を指していれば既存GitHub Release assetを再利用（§5）、tagより先行していれば
   `git-build`でreviewed importする。
3. 新しい`home-alpha-full@N+1`（append-only）を作成し、3成分のartifact
   digest/commitを更新する。
4. `mcrctl resolve/plan/render/apply/doctor`で通常のalpha applyフローを実行する
   （既存`home-alpha-validation-guide_ja.md`のunverified acknowledgementと
   同じ形）。
5. sanitized live-human evidenceを取得し、価値あるsliceが確認できれば
   §1の表に沿って次の`bN`をtagする判断を人間へ返す（Stackはtagを打たない）。

## 8. 未確定・要人間判断事項

- `tailscale serve`のtailnet側公開port番号を、Stackがrenderするloopback port
  （`scratch_port`/`bridge_port`）と一致させる運用を前提にしている。実機で
  `tailscale serve`を設定する際、この対応関係をrunbookに明記する必要がある。
- Minecraft本体（java_port/mcremote_port）もloopbackにしかbindされないため、
  tailnet参加client（生Minecraft protocolで接続するclient、Bridgeの
  `BRIDGE_SANDBOX_PORT`宛通信は同一Dockerネットワーク内なので対象外）から直接
  到達させる場合は、`tailscale serve`のTCPモード（HTTP以外のraw TCP転送）を
  併用するか、Minecraft接続はtailnet越しでなくCaddy/Scratch/Bridge経由の
  sandbox概念だけで完結させるかを人間が選ぶ必要がある。現状の実装は後者
  （`default_sandbox`をhostnameのみで表現し、実際のjava接続経路は別途human
  runbookで定義）を前提にしているが、明示合意はまだない。
- ~~`home-server@5`との重複~~ 確認済み。commit `448de60`
  （2026-08-21「Prepare normal dev deployment workflow」）で追加された
  `docs/normal-dev-environment-guide_ja.md`向けのprofileであり、terms-glossaryの
  「通常dev integration harness」（Stack自身の横断確認用の使い捨て暖機環境）に
  対応する。home private alpha（m720s1）とは別目的であり、本設計の`@6`と
  役割は衝突しない。
