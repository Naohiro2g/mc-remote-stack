# preset registry / preset catalog / lock 解決仕様

## 0. 文書の位置づけ

この文書は、`mc-remote-stack` における次世代 deployment 構成のうち、H
「preset registry / preset catalog / lock 解決仕様」の詳細設計 SSOT である。
実装規範を定める文書である。bundled profile / preset registry / preset catalog /
compatibility record の schema と loader、RFC 8785 content digest、catalog の安定生成・stale検出、
published revision の append-only 比較、preset 選択の resolver、TOML lock schema、
semantic identity、no-op / stale / tamper 検出、atomic replace、`preset list/show` と
TOML `init` / `resolve` / `validate` / `accept-eula` / `plan` / `artifact fetch` / `render`
CLI、最初の bundled home profile / preset、digest検証付き`compose@1` renderまで実装した。
typed operator input境界と最初の`minecraft-motd@1`も実装した。
current lockとcanonical renderに固定した初回bootstrap applyも実装した。
upgrade applyとplugin固有operator input mappingはまだ未実装であり、現行の
`mc-remote.yml` / `mc-remote.lock.yml` 経路は回帰fixtureとして残る。

- 状態: 実装済み（H。apply / plugin config ownership は対象外）
- 対象: preset registry、preset catalog、profile / preset / order の解決、lock identity、
  compatibility evidence、custom / unverified gate
- 関連する物理配置:
  [`mc-remote.toml` project layout / 物理ファイル粒度](toml-project-layout-design_ja.md)
- 対象外: upgrade transaction、backup / restore、world lineage、plugin config ownership、
  remote preset registry
- knowledge 参照 commit:
  `f1b99a049b6bc57799c3356c3e54d29e45031451`
- 主な根拠:
  `2026-07-21-03`、`2026-07-23-01`〜`2026-07-23-04`、`2026-07-23-06`

依頼時に提示された knowledge commit
`c1911db9a53620b707ea3d4e891d1924ef2109eb` は上記 commit の祖先である。
後続の `2026-07-23-06` に従い、deployment 構成では無修飾の
`catalog` / `registry` を使わず、必ず `preset catalog` / `preset registry`
（machine key は `preset_catalog` / `preset_registry`）と表記する。
wire の `catalogHash` が指す Minecraft resource catalog とは別概念である。

規範語の `MUST` / `MUST NOT` / `SHOULD` は、この文書内では実装と検証の必須度を表す。

## 1. 解決する問題

現行実装は、`official-vps` の topology、policy、instance 値、component 選択、
exact artifact identity を一組の YAML config / lock に保持している。この形では、
別 profile の home server を加えたときに、次の差を独立に説明しにくい。

- どこでどう動かすか: profile が持つ topology / policy
- 何を組み合わせるか: preset が持つ検証済み component 構成
- どの environment を望むか: human-owned order
- 実際に何へ固定されたか: machine-owned lock
- 現在どの preset を提供しているか: preset catalog
- 過去を含む preset revision の identity 源: preset registry

本仕様はこれらを分離し、同じ component 構成を複数 profile / environment へ投影でき、
かつ再解決・plan・render のたびに由来と検証状態を説明できるようにする。

## 2. 不変条件

実装は次を満たさなければならない。

1. **一つの identity 源**
   preset revision の identity 源は bundled preset registry だけとする。
   preset catalog、order、lock、artifact store は revision を新設しない。
2. **exact selection**
   order は profile と preset を必ず exact revision で選ぶ。
   `latest`、version range、branch、tag だけの参照、無固定 `HEAD` は選択子に使えない。
3. **append-only revision**
   `main` へ入った preset / profile revision は変更・削除しない。
   訂正は新 revision とする。
4. **preset catalog は投影**
   preset catalog は現在提供中の revision を示す生成物であり、過去 revision の
   identity や既存 lock の再現性を所有しない。
5. **lock は解決済み事実**
   lock は order の複製でも preset catalog snapshot でもない。profile、preset、override、
   resolver / renderer、source order を解決した exact result である。
6. **secret と runtime state を固定しない**
   lock は secret reference identity を含めるが secret 値を含めない。
   world、credential store、backup、ACME state、log、cache 等の runtime-owned state を含めない。
7. **fail closed**
   未知 revision、moving selector、profile 非互換、未許可 override、根拠のない互換主張、
   stale / tampered lock を暗黙補完しない。
8. **意味が同じなら lock を書き換えない**
   semantic な解決結果が同じ no-op resolve では、serialization や `resolved_at` を更新しない。
9. **環境名から意味を推測しない**
   `home-beta` 等の identity から channel、exposure、purpose、profile、preset を導かない。
10. **物理配置と意味論を分離**
    本仕様の order / environment lock は論理モデルである。F は一 environment 一 project を
    正準配置に選び、file layout が本仕様の identity 計算を変えないようにする。

## 3. 概念モデル

```text
bundled profile revision ─┐
                          ├─ resolver ──> environment lock ──> plan / render
bundled preset revision ──┤       │
                          │       └─ compatibility records
human-owned order ────────┘

preset registry ──> preset catalog generator ──> preset catalog
       │                                             │
       └── exact revision lookup <───────────────────┘ discovery only
```

### 3.1 deployment / environment

- deployment は instance identity と profile 選択を持つ。
- environment は `identity`、`channel`、`exposure`、`purpose` の独立した4軸を持つ。
- 一つの environment は一つの exact preset revision を選ぶ。
- profile / purpose / security policy が許す組合せは cross-field validation で確認する。

### 3.2 profile

profile は topology と policy template を定義する。component や artifact の版を選ばない。
profile 自身も `name@revision` で immutable に識別し、lock は revision と content digest の
両方を固定する。

profile は少なくとも次を宣言する。

- services / adapters / network shape / volume roles
- 対応可能な channel / exposure / purpose の組合せ
- 必須 security controls
- order が指定すべき instance 値
- preset に要求する component role / capability
- override 可能な logical path と、その変更が compatibility relevant か
- renderer 名と render-plan schema

### 3.3 preset

preset は名前と immutable revision を持つ、検証対象となる component 構成である。
profile の topology を複製せず、component role、exact artifact、compatibility claim、
必要 capability を宣言する。

### 3.4 order

order は一つの environment に対する human-owned な論理 desired state である。
exact profile / preset ref、4軸、instance 値、明示 override、acknowledgement を持つ。
コメントや人間向けの並びは order の編集面で保持するが、lexical layout は semantic identity
へ含めない。

### 3.5 lock

lock は一つの environment の解決結果を表す論理レコードである。F に従い、一つの
deployment project の `mc-remote.lock.toml` は一つの EnvironmentLock だけを持つ。

## 4. bundled data の配置

初期実装は trust root を増やさず、`mcrctl` と同じ source distribution / wheel に同梱した
preset registry と profile だけを読む。正準 source layout は次とする。

```text
src/mc_remote_stack/data/
├─ profiles/
│  └─ <profile-name>/<revision>/profile.toml
├─ preset_registry/
│  └─ <preset-name>/<revision>/preset.toml
├─ compatibility/
│  └─ records/<record-id>.toml
├─ preset_catalog_policy.toml
├─ preset_catalog.toml                 # generated; hand edit禁止
└─ schemas/
   ├─ profile.schema.json
   ├─ preset.schema.json
   ├─ compatibility-record.schema.json
   └─ lock.schema.json
```

この directory は package data として wheel / sdist に収録する。source tree と installed package
で別コピーを手維持してはならない。build / CI は次を検証する。

- schema validation
- path の name / revision と本文 identity の一致
- generated preset catalog と source の一致
- package artifact 内に同じ bytes が収録されること
- main 上の既存 revision が変更・削除されていないこと

remote URL、別 Git repository、user directory から preset registry を追加する機能は初期実装に
含めない。将来追加する場合は、trust root、署名、namespace collision、offline cache、
revocation を別設計で閉じるまで fail closed とする。

## 5. identity と revision

### 5.1 名前

profile / preset の name は `^[a-z0-9][a-z0-9-]{0,62}$` とする。
revision は先頭ゼロのない正の10進整数文字列とする。正準参照形は `<name>@<revision>` である。

例:

```text
home-server@1
classroom-paper@3
```

revision は artifact version や release channel ではない。revision 間の大小は登録順だけを表し、
互換性や推奨度を表さない。order は alias、range、`latest` を使用できない。

### 5.2 content digest

各 revision は本文の semantic canonical form に対する SHA-256 を持つ。identity は
`name@revision`、digest は tamper detection と lock の rerun 入力であり、どちらか一方で
代用しない。

既存 revision の bytes が変わっていても、本文内の revision を同じにしたまま受理してはならない。
CI は `main` の既存 path に対する edit / delete を拒否する。branch 上でまだ main に入っていない
新 revision は merge 前に修正してよい。

## 6. preset registry record

preset record は少なくとも次の論理 field を持つ。

```toml
schema_version = 1

[preset]
name = "classroom-paper"
revision = "3"
description = "Example only"

[requirements]
profile_capabilities = ["compose", "paper", "persistent-world"]
allowed_channels = ["beta", "stable"]
required_claims = ["profile-render"]

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
protocol = "21.0.0"

[[artifacts]]
id = "minecraft-image"
kind = "oci"
version = "example"
locator = "registry.example/minecraft"
digest = "sha256:<64-hex>"

[[artifacts]]
id = "paper-jar"
kind = "https-file"
version = "1.21.11-132"
filename = "paper-1.21.11-132.jar"
sha256 = "<64-hex>"
origin = "https://example.invalid/paper-1.21.11-132.jar"

[[artifacts]]
id = "mcremote-jar"
kind = "https-file"
version = "2100.0.0b2"
filename = "mc-remote-example.jar"
sha256 = "<64-hex>"
origin = "https://example.invalid/mc-remote-example.jar"
```

上の値は schema 形を示す例であり、実 artifact の採用決定ではない。
Paper component の `minecraft_version` は renderer が container の `VERSION` と
generated `server.properties` を一致させるための明示 target である。Paper artifact の
version 文字列や filename から推測しない。

### 6.1 record に入れないもの

- deployment / environment identity
- host、公開 IP、account、provider、費用等の backstage 情報
- secret 値
- runtime volume path の実値
- world / backup / credential state
- mutable な deprecation / EOL 状態
- 「現在のおすすめ」を表す moving pointer

### 6.2 alpha source artifact

alpha environment で tag 前 source を使う component は、次をすべて exact に固定する。

- repository identity
- full commit SHA
- source subdirectory
- build recipe identity / digest
- toolchain identity / digest
- build input digest
- output artifact SHA-256
- build provenance

branch、PR number、`HEAD`、short SHA だけでは artifact identity として不足する。
実験対象以外の component は既知 artifact に固定する。

## 7. exact artifact identity

artifact は kind ごとに次を満たす。

| kind | identity に必須 | 補足 |
| --- | --- | --- |
| OCI image | OCI registry / repository と manifest digest | tag は表示用 metadata にできるが解決入力にしない |
| HTTPS file | content SHA-256、filename、origin | URL が不変でも SHA-256 を省略しない |
| Git build | repository、full commit、recipe / toolchain identity、output SHA-256 | build 後の bytes まで固定する |
| recovery archive member | archive SHA-256、member path、output SHA-256 | archive 全体や未指定 member を展開しない |
| static archive | archive SHA-256、format、source provenance | 展開後に除外する host 固有 file も明示する |

version、release tag、Paper build number、package version は監査と表示に保持するが、それだけを
artifact identity にしない。resolver は digest 不足を network の現在値で補完してはならない。

### 7.1 world version safety boundary

exact artifact を古い Minecraft version へ戻せることと、その artifact で既存 world を開いてよい
ことは別である。lock は runtime-owned world state を含めず、artifact rollback を world downgrade
の許可として扱わない。新しい Minecraft version で開いた world を古い version で再利用する経路は
提供せず、将来の world lineage / checkpoint gate が compatible な pre-upgrade checkpoint、
fork、または新規 world を要求する。汎用 `--force` でこの境界を越えない。

## 8. preset catalog

### 8.1 役割

`preset_catalog.toml` は、bundled preset registry と
`preset_catalog_policy.toml` から生成する現在提供中の投影である。
`mcrctl preset list` / `mcrctl preset show` の discovery 面に使う。

human-owned な `preset_catalog_policy.toml` は lifecycle 事実だけを持つ。
profile capability、artifact、compatibility status を重複記述せず、generator が selected
preset / profile / compatibility record から join する。

```toml
schema_version = 1

[[presets]]
ref = "classroom-paper@3"
status = "active"
available_since = "2026-07-23"

[[presets]]
ref = "classroom-paper@2"
status = "eol"
available_since = "2026-07-01"
deprecated_since = "2026-07-15"
eol_since = "2026-07-23"
reason = "Example only"
replacement = "classroom-paper@3"
```

日付は lifecycle provenance であり、preset revision identity ではない。

preset catalog entry は少なくとも次を表示できる。

- exact preset ref と content digest
- lifecycle status
- supported profile ref / capability
- channel / exposure / purpose に関する明示 constraint
- deployment requirement
- effective compatibility status と record ref
- deprecation / EOL の理由と移行先 exact ref（存在する場合）

### 8.2 lifecycle

lifecycle status は次の3値から始める。

| status | discovery | 新規 resolve |
| --- | --- | --- |
| `active` | 通常表示 | 許可 |
| `deprecated` | 通常表示＋警告 | exact ref のみ許可＋警告 |
| `eol` | 明示 option で表示 | 既定拒否 |

EOL revision も preset registry から削除しない。既存 lock は preset catalog の更新だけで書き換えない。
EOL revision を新しく resolve する例外経路を実装する場合は、order 内 acknowledgement と
CLI の one-shot acknowledgement の両方を要求する。preset catalog が示す移行先も exact ref とし、
order を自動更新しない。

### 8.3 生成規律

- `preset_catalog.toml` は hand edit を拒否する。
- generator は stable serialization order を使う。
- 同一 source / tool version なら byte-identical に生成する。
- preset catalog の stale 差分を CI failure とする。
- preset catalog に載っていない過去 revision も exact ref で preset registry から検査できる。
- preset catalog entry の追加・削除は既存 order / lock を自動変更しない。

## 9. compatibility evidence

### 9.1 claim と evidence の分離

preset record は「この component 構成が何を要求するか」を宣言する。
「実際に互換性を確認した」という主張は immutable compatibility record が担う。
これにより、初回 live 検証後に preset record を改変せず evidence を追加できる。

compatibility record は次を exact に束縛する。

- record ID と schema version
- preset ref と preset content digest
- profile ref と profile content digest
- 対象 component / artifact identity の集合 digest
- 検証した protocol / Minecraft / Paper / loader / adapter 条件
- test class: `unit/deterministic`、`live-auto`、`live-human`
- PASS の根拠 path / URI と source commit。sanitized artifact がある場合はその SHA-256
- 検証日と、適用範囲を狭める constraint

論理 schema の最小形は次とする。

```toml
schema_version = 1

[record]
id = "home-server-classroom-paper-3"
test_class = "live-auto"
result = "pass"
verified_at = "2026-07-23T00:00:00Z"

[subject]
preset_ref = "classroom-paper@3"
preset_sha256 = "<64-hex>"
profile_ref = "home-server@1"
profile_sha256 = "<64-hex>"
component_set_sha256 = "<64-hex>"

[[claims]]
id = "profile-render"
constraint = "example only"

[[evidence]]
repository = "Naohiro2g/mc-remote-knowledge"
commit = "<full-commit-sha>"
path = "14-evidence/records/example_ja.md"
```

`result = "pass"` の immutable record だけが compatibility coverage を増やす。
失敗記録や調査ログは evidence policy に従って保存できるが、resolver が PASS と読み替えない。

`live-human` を public compatibility claim に使う場合は、knowledge の evidence policy に従い、
sanitized evidence record / artifact を参照する。token、pair code、private host、UUID、
secret 値を record や lock に複製しない。

bundled compatibility record が `verified` を支える根拠は、public contributor が backstage や
frozen archive を読まずに監査できなければならない。backstage-only evidence は private な
運用判断の補助には使えても、bundled preset catalog の public な verified 表示には使わない。

### 9.2 verification の判定

resolver は required claim を列挙し、それぞれを deterministic rule または compatibility record
へ結びつける。少なくとも次を別 claim として扱う。

- profile が要求する component role が揃う
- plugin / client の protocol 互換
- plugin artifact と Minecraft / Paper / loader target の互換
- renderer / adapter が profile revision を扱える
- security / exposure の cross-field constraint

exact な解決結果の全 required claim が coverage を持つときだけ
`compatibility.status = "verified"` とする。次を verified とみなしてはならない。

- component version が同じ `bN` らしく見える
- environment identity に `beta` / `alpha` が含まれる
- preset catalog に表示されている
- 過去の似た artifact 組合せが動いた
- evidence path / commit を示さない手動 boolean

### 9.3 custom と unverified

次を分けて lock に記録する。

- `selection.kind = "preset"`: preset の compatibility-relevant 部分を変更していない
- `selection.kind = "custom"`: component / artifact または compatibility-relevant policy を override した
- `compatibility.status = "verified"`: exact result の required claim が evidence で覆われる
- `compatibility.status = "unverified"`: coverage が不足する

custom でも exact result を覆う record があれば verified にできる。preset 選択でも evidence が
不足すれば unverified である。二つの軸を一つの `custom = true/false` へ畳まない。

## 10. order と override

F を閉じるまでは、次を一 environment の論理 order とする。

```toml
schema_version = 1

[deployment]
name = "home"
profile = "home-server@2"

[environment]
identity = "home-beta"
channel = "beta"
exposure = "<must-be-explicit>"
purpose = "<must-be-explicit>"
preset = "classroom-paper@3"

[runtime]
artifact_store = "/var/lib/mc-remote/artifacts"

[[runtime.volumes]]
role = "minecraft-data"
identity = "home-beta-minecraft-data"

[world]
identity = "home-beta-world"

[network]
bind_address = "127.0.0.1"
java_port = 25565
mcremote_port = 25575

[agreements]
minecraft_eula = false

[acknowledgements]
allow_unverified = false
unverified_reason = ""
allow_eol = false
eol_reason = ""

[[operator_inputs]]
role = "minecraft-motd"
adapter = "minecraft-motd@1"
path = "operator/minecraft-motd/server.properties"
```

placeholder は文書例であり有効値ではない。実際の order では4軸をすべて明示する。
acknowledgement を `true` にするときは対応する reason を空にできない。
EULA は acknowledgement override ではなく deployment agreement である。`false` の order は
unresolved state として有効だが、resolver は `minecraft_eula_not_accepted` で停止する。
`operator_inputs`は選択profileがrole / adapterを宣言した場合だけ指定できる任意入力である。
`home-server@2`の`minecraft-motd@1`は公開表示文だけを扱い、secret injection pointを持たない。

volume / world の identity は instance-owned desired reference として lock するが、volume path の
実体、world bytes、player data は runtime-owned state のままで lock に含めない。

### 10.1 merge 順序

resolver の source precedence は低い順に固定する。

1. profile revision の topology / policy default
2. preset revision の component 構成
3. order の instance 値
4. order の明示 override

後段が任意の前段値を上書きできるわけではない。profile / preset が宣言した allowlist の path
だけを override できる。unknown path、owner の違う path、security control を弱める override は
拒否する。

### 10.2 override class

| class | 例 | selection / evidence への影響 |
| --- | --- | --- |
| instance | hostname ref、port、volume role の割当 | profile の allowlist 内なら preset のまま |
| operational | memory、schedule、capacity | compatibility relevance は profile schema が宣言 |
| component | plugin の追加削除、artifact 差替え | custom。exact coverage が無ければ unverified |
| security | auth、TLS、network exposure、permission policy | constraint を弱める override は拒否。許可変更も再検証 |
| runtime-owned | world、credential、backup bytes | order / lock override の対象外 |

secret は override class ではない。`secret://<id>` の reference identity だけを order から解決し、
material の値は secret store が apply / runtime 時に提供する。

## 11. resolver

### 11.1 入力

resolver が読む正準入力は次だけとする。

- logical order
- order から exact path で明示参照された operator-owned input
- bundled exact profile revision
- bundled exact preset revision
- bundled compatibility records
- resolver / renderer 自身の schema と version

preset catalog は discovery と lifecycle gate に使うが、preset 本文の代わりにしない。
secret store、runtime directory、稼働 server の観測値、network の `latest` は lock 解決入力にしない。

### 11.2 deterministic pipeline

`mcrctl resolve` は次の順序で処理する。

1. order schema と4軸の存在を検証する。
2. exact profile ref / preset ref の構文を検証する。
3. bundled data から revision を引き、path / identity / content digest を照合する。
4. operator-owned input の explicit reference、owner、path、adapter semantic digest を検証する。
5. preset catalog policy の lifecycle gate を評価する。
6. profile capability と preset requirement を照合する。
7. 固定 precedence と allowlist で logical desired state を merge する。
8. channel / exposure / purpose / network bind / volume role / security の cross-field validation を行う。
9. 全 artifact が exact identity を持つことを検証する。
10. required compatibility claim と coverage を計算する。
11. EULA agreement と custom / unverified / EOL acknowledgement gate を評価する。
12. secret reference identity を列挙し、secret 値が混入していないことを検査する。
13. renderer adapter を使い operator input の semantic digest と non-secret render plan を作る。
14. candidate EnvironmentLock と semantic identity を生成する。
15. 既存 lock と identity が同じなら何も書かず no-op とする。
16. 異なる場合だけ `resolved_at` を設定し、temporary file、fsync、atomic replace で lock を更新する。

途中で失敗した場合、既存 lock を削除・部分更新しない。candidate は診断表示に使えても
正準 lock path へ書かない。

### 11.3 stable diagnostics

少なくとも次の reason を機械的に区別する。

- `unknown_profile_revision`
- `unknown_preset_revision`
- `mutable_selector`
- `registry_record_tampered`
- `preset_eol`
- `profile_incompatible`
- `unsupported_environment_combination`
- `override_not_allowed`
- `artifact_identity_incomplete`
- `compatibility_evidence_missing`
- `unverified_not_acknowledged`
- `minecraft_eula_not_accepted`
- `secret_value_forbidden`
- `operator_input_profile_mismatch`
- `operator_input_parse_failed`
- `operator_input_secret_forbidden`
- `stale_lock`
- `lock_identity_mismatch`

人間向け message は reason、対象 logical path、修正方法を示し、値が secret の可能性がある path
では実値を表示しない。

## 12. unverified / EOL gate

初期実装は二段 acknowledgement とする。

1. order に理由付きの永続 acknowledgement を置く。
2. resolve / 将来の apply 実行時に one-shot CLI flag を渡す。

たとえば unverified を許可する場合、order の `allow_unverified = true` だけでも
`--allow-unverified` だけでも足りない。両方がそろったときだけ lock を生成できる。
lock は acknowledgement、reason、compatibility status を記録し、plan は常に目立つ warning を出す。

この gate は security validation を無効にしない。unverified を許可しても、secret 混入、
unsupported exposure、TLS / auth / network policy 違反、moving selector は引き続き拒否する。

## 13. lock schema

EnvironmentLock は少なくとも次を保持する。

```toml
schema_version = 1
lock_identity = "sha256:<64-hex>"
resolved_at = "2026-07-23T00:00:00Z"

[resolver]
name = "mcrctl"
version = "<exact-tool-version>"
lock_schema = 1
canonicalization = "jcs-rfc8785-v1"

[input.order]
semantic_sha256 = "<64-hex>"

[input.profile]
ref = "home-server@1"
content_sha256 = "<64-hex>"

[input.preset]
ref = "classroom-paper@3"
content_sha256 = "<64-hex>"

[selection]
kind = "preset"

[compatibility]
status = "verified"
required_claims_sha256 = "<64-hex>"

[[compatibility.records]]
id = "example-record"
content_sha256 = "<64-hex>"
source_commit = "<full-commit-sha>"
source_path = "14-evidence/records/example_ja.md"

[[secret_references]]
id = "example-secret"
usage = "example-adapter"

[[artifacts]]
component = "example-component"
kind = "oci"
version = "example"
locator = "registry.example/component"
digest = "sha256:<64-hex>"

[render_plan]
adapter = "example-renderer"
adapter_revision = "1"
semantic_sha256 = "<64-hex>"
```

実 lock はこのほか、次を含む。

- deployment / environment の4軸と non-secret identity
- source precedence と、各 source の semantic digest
- resolve 済み non-secret desired state
- exact artifact identity と取得 / build provenance
- profile / preset constraint の評価結果
- acknowledgement と verification result
- non-secret render plan
- runtime-owned data と secret-injected bytes が保証外であることを示す scope marker

`resolved_at` は provenance であり identity ではない。同じ identity の candidate に対して
時刻だけを更新してはならない。

### 13.1 lock に入れないもの

- secret 値
- secret 注入後にだけ確定する generated bytes
- world / player / credential / backup 内容
- process ID、container ID、current health、cache
- provider account、private host inventory
- preset catalog の表示順や説明文
- TOML のコメント、空白、quote style、key 順序

## 14. semantic canonicalization

hash 前の論理値を I-JSON 互換の data model へ投影し、
[RFC 8785 JSON Canonicalization Scheme](https://www.rfc-editor.org/rfc/rfc8785.html) で
UTF-8 bytes に直列化する。この project 内での規則名を `jcs-rfc8785-v1` とする。
これは JSON file を新しい SSOT として追加する意味ではなく、TOML の lexical 差を除いた
hash input を一意にする内部規則である。

規則は次とする。

1. object property の recursive sort、string escape、空白なし serialization、UTF-8 encoding は
   RFC 8785 に従い、独自実装差を許さない。
2. string、integer、boolean、array、table だけを identity-bearing model で許可する。
3. integer は絶対値が `2^53 - 1` 以下に収まる値だけを許可する。それを超える識別値は string とする。
4. float と TOML native date / time は禁止する。時刻は UTC の正準 string、duration は単位付き
   field または integer とする。
5. string は Unicode normalization で別値へ変換せず、入力の scalar sequence を保持する。
6. array 順序は原則 semantic として保持する。
7. schema が set と宣言した collection だけを、その record identity で sort してから hash する。
8. map の元の key 順、コメント、空白、quote style は捨てる。
9. secret reference ID は含め、secret material は入力段階で拒否する。

`lock_identity` は、次の identity payload を canonicalize した SHA-256 とする。

- lock schema / canonicalization version
- resolver / renderer exact version
- order semantic digest
- profile ref / content digest
- preset ref / content digest
- source precedence
- resolve 済み non-secret desired state
- secret reference identity
- exact artifacts / provenance
- compatibility claims / records / acknowledgement
- non-secret render plan

`resolved_at`、serializer version、source file path、preset catalog の current metadata は除外する。
F で file 数や配置が変わっても logical order が同じなら order semantic digest は変わらない。

## 15. stale / tamper / no-op

### 15.1 no-op

candidate の `lock_identity` が既存 lock と一致する場合:

- lock file を書き換えない
- `resolved_at` を更新しない
- exit は success
- plan には `lock=unchanged` と表示できる

### 15.2 stale

order、profile、preset、tool、compatibility coverage、render plan のいずれかが変わり
candidate identity が異なる場合、既存 lock は stale である。plan / render は、order と lock の
input digest が一致しなければ停止し、先に明示的な resolve を要求する。

### 15.3 tamper

次は stale ではなく tamper / corruption として停止する。

- lock 本文から再計算した identity が `lock_identity` と一致しない
- selected preset / profile の `name@revision` は同じだが content digest が preset registry と違う
- artifact store の content が lock digest と違う
- generated preset catalog が generator の出力と違う

tamper 時に network から「正しそうな最新版」を取得して自動修復しない。
`artifact fetch` も既存のcontent-addressed entryが不一致なら上書きせず停止する。

## 16. CLI surface

H の実装で追加する正準 surface は次とする。

```text
mcrctl init <path> --format toml <all-instance-arguments>
mcrctl preset list
mcrctl preset show <name>@<revision>
mcrctl validate --project <path>
mcrctl accept-eula --project <path> --yes
mcrctl resolve --project <path> [--allow-unverified] [--allow-eol]
mcrctl plan --project <path>
mcrctl artifact fetch --project <path>
mcrctl render --project <path> --output <path>
mcrctl apply --project <path> --output <path> \
  --expected-lock-identity <identity> --docker-context <local-context> \
  --bootstrap --yes [--allow-unverified] [--allow-eol]
mcrctl doctor --project <path> [--output <path>] \
  [--docker-context <local-context>] [--timeout <seconds>]
```

- top-level `mcrctl catalog` / `mcrctl registry` は作らない。
- TOML `init` は全instance値を明示入力にし、EULA=falseのorderだけを作る。lockやartifact
  storeを作らない。legacy YAML initは移行期間中、`--format legacy-yaml`（default）として分離する。
- `preset list` は preset catalog を読む。
- `preset show <exact-ref>` は preset registry record と lifecycle / compatibility 投影を表示する。
- `resolve` だけが machine-owned lock を更新する。
- `plan` / `artifact fetch` / `render` は selector を再解決せず、order と lock の一致を検証して
  lockを使う。
- `apply` はrender済みbytesの由来となるlock identityを必須入力にし、初版は
  `home-server@2` / `mcremote-paper@1`のisolated beta bootstrapだけを受理する。
- `doctor`は既定の`<project>/generated`とlocal context `default`をread-only確認し、applyを
  status commandとして再利用しない。
- TOML `init`はproject rootを最大`0750`、初期order / README / `.gitignore`を最大`0640`で作る。
  呼出し元umaskが厳しい場合は緩めず、既存の非空projectや後続のvalidate / doctorがpermissionを
  暗黙変更しない。projectはsecret storeではないが、Docker権限で消費するtrusted inputとして
  非管理主体からの書込みを許さない。

### 16.1 lock-backed artifact fetch

`mcrctl artifact fetch --project <path>` は、current orderに対して`unchanged`なlock内の
`kind = "https-file"` だけを取得する。

- originはcredentialを含まないHTTPS URLだけを許可し、redirect後の最終URLもHTTPSを要求する。
- operatorがURL、version、`latest`、digest、store overrideをCLIから差し替えるsurfaceは持たない。
- responseはstreamし、512 MiBを上限とする。`Content-Length`が無い場合も実byte数で上限を守る。
- temporary fileへ書きながらSHA-256を計算し、lockと一致したbytesだけを
  `<runtime.artifact_store>/sha256/<sha256>` へpublishする。
- existing entryはnetwork access前に毎回再hashする。一致すれば`present`、不一致・symlink・
  非regular fileなら`artifact_store_tampered`で停止し、自動上書きしない。
- publishは同一filesystem内のatomicなcreate-if-absentとし、並行writerが先に同じdigestを
  作った場合はそのentryを再hashする。
- digest不一致、size超過、download失敗ではtemporary fileを除去し、destinationを作らない。
- OCI artifact、git build、recovery archiveをこのcommandで暗黙取得・build・展開しない。
- missing / stale / tampered lockではstore directory作成やnetwork accessより前に停止する。

fetchはartifact bytesをCASへ準備するだけであり、OCI pull、Compose起動、volume作成、
server接続、applyを行わない。

### 16.2 `compose@1` render contract

`home-server@1` / `home-server@2` のrendererは`compose@1`とし、TOML projectでは次だけを生成する。

```text
<output>/
├─ compose.yaml
├─ minecraft/
│  └─ server.properties
└─ render-manifest.json
```

- 入力は current order と、それに対して `unchanged` な lock だけとする。selector を再解決しない。
- `minecraft-runtime` は `locator:version@digest` のOCI referenceとして出力し、digestを省略しない。
- `paper-server.minecraft_version` を container `VERSION` へ出力する。artifact versionから導出しない。
- Paper / McRemote file は `<artifact_store>/sha256/<digest>` を再hashし、一致したものだけを
  read-only bind mountする。欠落は `artifact_missing`、不一致は `artifact_tampered` で停止する。
- `minecraft-data` はorderの明示identityを持つexternal named volumeとし、world identityは
  `LEVEL`、`server.properties`、labelへ同じ値を投影する。
- `isolated` / `lan-only` のbind addressとhost portをCompose `ports`へそのまま投影する。
- `online-mode=true`、`enable-rcon=false`、EULA acceptanceを生成物でも維持する。
- optionalな`minecraft-motd@1`がlockにある場合、source fileを再読込せず、lock済みsemantic
  valueだけを`server.properties`の`motd`へ投影する。source commentや空白は生成物へcopyしない。
- `render-manifest.json` はadapter revision、lock identity、render-plan digest、各生成fileの
  path / SHA-256を持つ。時刻やhost観測値を入れない。
- 二回目の同一renderはbytes / mtimeを変えない。既存outputを置換できるのは、manifestと全file
  digestが一致し、未知fileを含まないmanaged outputだけである。
- replacementはsibling staging directoryで完成させてからrenameし、publish失敗時は旧managed
  outputへrollbackする。project root、その祖先、artifact storeと重なるoutputは拒否する。
- renderはCompose起動、volume作成、artifact取得、server接続を行わない。

`PAPER_CUSTOM_JAR` が指定されると公式imageはそのJARをserver entrypointとして使う一方、
`VERSION` は設定生成等でも参照されるため両方を明示する。この挙動は
[itzg/docker-minecraft-server `start-deployPaper`](https://github.com/itzg/docker-minecraft-server/blob/99d4481c01559a40554f2628a433cded62f322cc/scripts/start-deployPaper)
と[Paper server type documentation](https://github.com/itzg/docker-minecraft-server/blob/master/docs/types-and-platforms/server-types/paper.md)
に基づく。

### 16.3 bootstrap apply contract

初回live applyは、current lock、canonical render bytes、artifact digest、review済みlock identityを
Docker接続前に照合する。対象host上の明示local Unix Docker contextだけを許可し、exact image pull、
managed external volume作成、Compose `--wait`起動、lock label postcheckを行う。

既存のunknown container / volume、port衝突、別lock、remote Docker contextはfail closedとする。
起動失敗時はCompose containerをdownするが、runtime-owned world volumeを削除しない。
詳細は[`home-beta` bootstrap apply設計](home-beta-bootstrap-apply-design_ja.md)を正とする。

### 16.4 read-only doctor contract

`mcrctl doctor --project <path>`はcurrent lockとcanonical renderをDocker接続前に照合し、
local Unix Docker context、managed volume、exactly oneのcurrent container、running / healthy、
lockどおりのpublish portを確認する。既定outputは`<project>/generated`、既定contextは`default`で、
必要な場合だけ明示overrideする。

runtime preflight後、lockのplugin protocol / Minecraft version / world identityを使い、
McRemote portへLF終端のtoken無しJSON-RPC `hello`を送る。public response fieldだけを照合し、
認証強制時の`auth_required`はresponsiveとして区別する。生response、container log、
session / player / tokenを通常出力へ載せない。doctorはDocker変更commandを実行せず、
selector解決、artifact取得、render置換、container再起動を行わない。

doctor PASSはcurrent runtimeと最小helloの整合だけを示し、compatibility verified、pairing、
実player操作、全command、backup / restore、upgrade、公開networkを主張しない。

plan は少なくとも次を operator に見せる。

- deployment / environment の4軸
- profile ref / preset ref / content digest
- selection kind と compatibility status
- preset lifecycle status
- artifact identity の変更
- volume / world role の変更
- security-relevant diff
- secret reference の追加削除（値は非表示）
- operator inputのrole、adapter、path、semantic SHA-256
- lock identity と no-op / stale / replacement
- blocker / warning と stable reason

## 17. home server への適用順

新設計の最初の live environment identity は `home-beta` とする。ただし名前から属性を推測せず、
order に channel、exposure、purpose、profile、preset を明示する。

実装と検証は次の順に進める。

1. この H 仕様に対する schema / resolver の失敗テストを先に追加する。
2. bundled preset registry / preset catalog / compatibility record loader を実装する。
3. lock canonicalization、no-op resolve、stale / tamper detection を実装する。
4. F の一 environment 一 project layout と lossless editor を実装する。
5. `home-server@1` profile と、`home-beta` が選ぶ exact preset revision を追加する。
6. TOML init、lock-backed artifact fetch、deterministic plan / render fixture を通す。
7. unverified bootstrap が必要なら二段 gate を明示して `home-beta` だけを live integration する。
8. `live-auto`、必要な `live-human` の sanitized evidence を作り、compatibility record を追加して
   verified lock へ再解決する。
9. `home-alpha` は後から別 runtime volume・別 world として追加する。

### 17.1 最初の bundled revision

最初に登録する実 revision は次とする。

- profile: `home-server@1`
  - Compose 上の単一 Paper service
  - channel は `beta` / `alpha`
  - exposure は `isolated` / `lan-only`
  - purpose は `integration`
  - required security controls は `online-mode` / `rcon-disabled`
- preset: `mcremote-paper@1`
  - Minecraft / Paper `1.21.11` build `132`
  - McRemote `2100.0.0b2`、protocol `21.0.0`
  - `itzg/minecraft-server:2026.7.2-java21`
  - channel は `beta` のみ

artifact identity は preset record の manifest digest / SHA-256 を正とし、tag、build番号、
version文字列だけでは採用しない。Paper と McRemote JAR は配布bytesのSHA-256を再計算し、
OCI image は registry の OCI index digest と照合した。

初回rolloutではcompatibility recordがなく`unverified`として二段gateを通した。その後の
sanitized live evidenceにより、exact subject `home-server@2` + `mcremote-paper@1`には
`home-server-2-mcremote-paper-1-live-auto`を追加した。現在この組合せは`verified`であり、
通常のresolve / applyに`--allow-unverified`は不要である。異なるprofile revisionやcomponent
setへcoverageを流用しない。

`home-alpha` は F に従って別 deployment project / order / lock とする。volume role と
world identity を `home-beta` と共有してはならない。

`official-vps` は当面 live apply 対象ではなく、現行 YAML 実装から得た plan / render の
deterministic regression fixture として扱う。新設計への明示変換では、既存 exact artifact
identity から lock を再生成し、validate / plan / render の差を比較する。旧 YAML reader を
長期互換経路として残したり、YAML と TOML の暗黙優先を設けたりしない。

## 18. test-first 実装 gate

コード着手時は、少なくとも次の test を失敗状態で先に追加する。

### preset registry / preset catalog

- exact ref の正常読込
- alias / `latest` / range / branch の拒否
- path と record identity 不一致の拒否
- main 上の revision edit / delete の拒否
- generated preset catalog の byte stability
- active / deprecated / EOL gate
- preset catalog と Minecraft resource catalog の machine key 非衝突

### resolver / override

- profile → preset → order → override の固定 precedence
- unknown / non-allowlisted override の拒否
- security control を弱める override の拒否
- environment identity から4軸を推測しない
- unsupported cross-field combination の拒否
- exact artifact identity 不足の拒否
- alpha source の full commit / recipe / output hash 必須

### compatibility

- required claim の coverage が揃ったときだけ verified
- preset でも evidence 不足なら unverified
- component override で custom になる
- custom result でも exact evidence があれば verified
- order acknowledgement と CLI flag の片方だけでは拒否
- secret / private host を evidence summary や lock に複製しない

### lock

- lexical に異なる同義 TOML が同じ semantic digest になる
- semantic change で identity が変わる
- `resolved_at` だけでは identity が変わらない
- no-op resolve が file の mtime / bytes を変えない
- stale order / lock の plan / render 拒否
- lock 本文改変の検出
- secret reference identity は hash に入り、secret 値は拒否される
- runtime-owned state が lock に入らない

### render

- stale / tampered lock では生成しない
- artifact store の欠落 / digest不一致で既存outputを変更しない
- OCI image・Paper target・JAR mount・volume / world・bind port・security controlをexactに投影する
- manifestが同じ二回目のrenderはbytes / mtimeを変更しない
- unmanaged / tampered outputを上書きしない
- managed replacementのpublish失敗で旧outputへrollbackする
- project / artifact storeとoutputのpath overlapを拒否する
- unit testやrendererからlive deployment commandを実行しない

### artifact fetch

- missing / stale / tampered lockでnetwork accessとstore作成をしない
- existing CAS entryを再hashし、一致時はnetwork accessとmtime更新をしない
- existing CAS entryの不一致を上書きしない
- HTTPSからHTTPへのredirect、credential付きURLを拒否する
- `Content-Length`の有無にかかわらずsize上限を守る
- download digest不一致でdestinationとtemporary fileを残さない
- lockにあるHTTPS fileだけを取得し、OCI pullやlive commandを実行しない

### rollout fixture

- `official-vps` の明示変換後 plan / render 比較
- `home-beta` の別 volume / world role
- 後続 `home-alpha` と volume / world が共有されないこと
- live deployment command を unit test や fixture から実行しないこと
- permissive umaskや既存の空directoryでも、新規TOML projectが`0750`、初期fileが`0640`より
  広くならないこと

## 19. H の完了条件と F への接続

H は、次が test と実装で満たされた時点で完了とする。

- bundled preset registry が immutable revision の identity 源になっている
- preset catalog が生成投影であり、CLI の preset namespace からだけ参照される
- exact profile / preset / artifact resolution が fail closed である
- compatibility evidence と custom / unverified gate が lock に投影される
- semantic lock identity、no-op、stale、tamper の挙動が固定される
- current lockだけを入力にHTTPS file artifactをcontent-addressed storeへ安全に取得できる
- secret 値と runtime-owned state が lock に入らない
- source tree と wheel / sdist の preset registry bytes が一致する

F は H の logical order / EnvironmentLock を変えず、一 environment 一 project、
generic include なし、explicit project discovery、lossless order editing、YAML / TOML 同居の
fail-closed、typed operator inputを実装し完了した。詳細は
[`mc-remote.toml` project layout / 物理ファイル粒度](toml-project-layout-design_ja.md)を正とする。
