# `mc-remote.toml` project layout / 物理ファイル粒度

## 0. 文書の位置づけ

この文書は、`mc-remote-stack` の次世代 deployment 構成における F
「`mc-remote.toml` の物理ファイル粒度」の詳細設計 SSOT である。
[preset registry / preset catalog / lock 解決仕様](preset-resolution-design_ja.md) が定義する
logical order / EnvironmentLock を、project directory と実 file へどう配置するかを定める。

- 状態: 実装済み（F。apply / plugin config ownership は対象外）
- knowledge 参照 commit:
  `f1b99a049b6bc57799c3356c3e54d29e45031451`
- 主な根拠:
  `2026-07-21-03`、`2026-07-23-01`、`2026-07-23-02`
- 対象:
  order / lock の物理単位、project discovery、include 方針、owner 分離、lossless editing、
  YAML / TOML 同居検出
- 対象外:
  profile / preset / lock の意味論、plugin config ownership の個別 mapping、
  upgrade transaction、複数 project の host-level transaction、world lineage

この文書は現行 layout の規範である。`toml_project` 層では exact project root の読込、
一 environment schema、YAML / TOML 同居 gate、placeholder lock を作らない初期化、
lossless な限定更新、TOML `resolve` / `validate` / `plan` / `render` を実装した。
TOML `compose@1` renderはgenerated outputだけを作り、live runtimeへ適用しない。
`--format toml` のoperator-facing `init` とlock-backed artifact fetchも実装済みである。
typed operator input境界と最初の`minecraft-motd@1` adapterも実装済みである。
current lockとcanonical renderに固定した初回bootstrap applyも実装済みである。
plugin固有mapping、host-level collision check、upgrade applyは未実装であり、
全体がmigration済みとはみなさない。

## 1. 決定

正準粒度を次に固定する。

> **1 environment = 1 order = 1 deployment project = 1 `mc-remote.toml`**

一つの project root は、一つの deployment instance と一つの environment だけを記述する。
machine-owned な `mc-remote.lock.toml` も同じ一 environment だけを固定する。

- 一つの `mc-remote.toml` に複数 environment を配列・table 群として置かない。
- 一つの project root に複数の order file を置かない。
- environment ごとに sibling project を作る。
- generic include / import / glob / environment variable interpolation は持たない。
- profile / preset の exact revision 参照を、再利用の正準機構とする。
- project directory 名は人間向けの便宜であり、identity の正本にしない。
- `deployment.name` と environment 4軸は `mc-remote.toml` に必ず明示する。
- artifact store、runtime volume identity、world identity、bind address / port は instance 値として
  `mc-remote.toml` に必ず明示する。directory 名から導出しない。
- EULA 同意は `[agreements].minecraft_eula` に明示し、初期値 `false` から専用 command で更新する。

`home-beta` は最初の live deployment project とし、`home-alpha` は後から別 project として追加する。
二つは order、lock、generated output、runtime volume、world identity を共有しない。

## 2. 選択肢の比較

| 観点 | 1 deployment file に複数 environment | 1 environment 1 project（採用） |
| --- | --- | --- |
| knowledge の order 定義 | 「一 environment の desired state」とずれる | そのまま一致 |
| plan / resolve / apply 境界 | environment が相互に巻き込まれる | environment ごとに独立 |
| lock no-op | alpha 追加で beta を含む lock 全体が変わる | beta lock は不変 |
| custom / unverified gate | 一 environment の実験が他へ波及 | 該当 environment だけで閉じる |
| TOML の深さ | array of tables と nested override が増える | 単数 table で浅い |
| comment-preserving edit | table placement の扱いが複雑 | 対象 logical path が一意 |
| shared host 値 | 重複を減らせる | 必要値は明示的に重複し得る |
| port / volume 競合 | 単一 plan で比較しやすい | host-level planner が別途必要 |
| partial failure | 他 environment も plan / resolve を阻害 | project 内だけで停止 |
| Git review | unrelated environment 差分が同居 | environment 単位で比較可能 |

shared host 値の重複は、profile / preset に属する再利用可能な値をそこへ移すことで減らす。
instance 固有値が少量重複する場合は、隠れた include dependency より明示重複を選ぶ。
複数 project の port / volume / Compose project 名の競合は無視せず、同一 host へ二つ目を apply する
前に host-level plan gate を要求する。これは generic include を導入する理由にはしない。

## 3. 正準 project layout

```text
<project-root>/
├─ mc-remote.toml              # human-owned order、tracked
├─ mc-remote.lock.toml         # machine-owned lock、resolve成功後に生成、tracked
├─ operator/                   # 明示参照されたoperator-owned入力、任意、tracked
│  └─ minecraft-motd/
│     └─ server.properties     # minecraft-motd@1の任意入力
├─ generated/                  # machine-owned render output、ignored
│  ├─ compose.yaml
│  ├─ minecraft/server.properties
│  └─ render-manifest.json
├─ .gitignore                  # tracked
└─ README.md                   # tracked、人間向けproject説明
```

secret store、artifact store、runtime volume、world、backup は project root の外に置く。
project root 内の `secrets/`、`backup/`、`world/`、runtime data directory は禁止する。

### 3.1 owner matrix

| surface | owner | Git | 更新方法 |
| --- | --- | --- | --- |
| `mc-remote.toml` | human | tracked | hand edit または lossless な限定更新 |
| `mc-remote.lock.toml` | resolver | tracked | `mcrctl resolve` のみ |
| `operator/**` | operator | tracked | hand edit。order から明示参照 |
| `generated/**` | renderer | ignored | `mcrctl render` で全生成 |
| secret material | secret store | Git外 | `mcrctl secret` 等の専用経路 |
| artifact bytes | artifact store | Git外 | digest 検証済み import / fetch |
| runtime-owned state | runtime | Git外 | server / adapter |

一つの path を複数 owner が更新してはならない。特に renderer は `mc-remote.toml` や
`operator/**` を変更せず、operator は lock / generated output を hand edit しない。

`render-manifest.json` はgenerated outputのowner markerでもある。既存outputにmanifestがない、
manifestに無いfileがある、またはfile digestが一致しない場合、rendererは上書きしない。
managed outputのreplacementはsibling stagingで完成させ、publish失敗時は旧directoryへ戻す。
project root / ancestorとartifact storeに重なるoutputは拒否する。

### 3.2 `.gitignore`

project template は少なくとも次を ignore する。

```gitignore
/generated/
/secrets/
/backup/
/backups/
/world/
/.env
*.secret
*.zip
*.tar
*.tar.gz
```

`mc-remote.toml`、`mc-remote.lock.toml`、`operator/` は ignore しない。
secret / backup / world を project root に置くこと自体が禁止であり、ignore は accidental add を
防ぐ defense in depth である。repo check は既知の forbidden directory が存在する場合、
ignored かどうかにかかわらず報告する。

## 4. `mc-remote.toml`

### 4.1 file contract

- UTF-8、BOM なし、TOML 1.0.0 subset の文書とする。
- project root に exact name `mc-remote.toml` で一つだけ置く。
- top-level は一つの logical order を表す。
- environment は singular table `[environment]` とし、`[[environments]]` を作らない。
- English configuration key だけを schema key に使う。
- unknown key は warning で無視せず schema error とする。
- secret material を書かず、`secret://<id>` の reference identity だけを書く。
- runtime observation を desired state として自動追記しない。

最小形の例:

```toml
schema_version = 1

[deployment]
name = "example-deployment"
profile = "home-server@1"

[environment]
identity = "example-environment"
channel = "beta"
exposure = "isolated"
purpose = "integration"
preset = "classroom-paper@3"

[runtime]
artifact_store = "/var/lib/mc-remote/artifacts"

[[runtime.volumes]]
role = "minecraft-data"
identity = "example-minecraft-data"

[world]
identity = "example-world"

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
```

この値は schema 例であり、`home-beta` の exposure / purpose / preset を決めるものではない。
directory basename と `deployment.name` / `environment.identity` の一致は要求しない。
一致・不一致のどちらからも channel、exposure、purpose を推測しない。

### 4.2 instance-owned contract

- `runtime.artifact_store` は `/` 以外の absolute POSIX path とし、`\` と `..` を拒否する。
- `runtime.volumes` は profile の `volume_roles` と role 集合が完全一致しなければならない。
  role と identity の重複を拒否する。
- `world.identity` は world bytes や path ではなく、runtime-owned world を指す非 secret identity である。
- volume / world identity は英小文字、数字、hyphen からなる明示 token とする。
- `network.bind_address` は hostname や wildcard でなく IPv4 address を明記する。
- port は `1..65535` で、Java と McRemote の port は一致させない。
- `isolated` は loopback bind だけを許可する。`lan-only` は RFC 1918
  (`10/8`、`172.16/12`、`192.168/16`) の bind だけを許可する。
- `agreements.minecraft_eula = false` の order は初期化・検証できるが resolve できない。
  `mcrctl accept-eula --project <root> --yes` だけが既存 scalar を lossless に `true` へ更新する。
- resolve 後の lock はこれらの identity / endpoint / agreement を含め、render plan digest と
  lock identity の入力にする。volume / world の実データは引き続き lock scope 外である。

### 4.3 一つの environment

次の形は拒否する。

```toml
[[environments]]
identity = "home-beta"

[[environments]]
identity = "home-alpha"
```

`home-alpha` を追加するときは新しい project root と新しい `mc-remote.toml` を作る。
既存 `home-beta/mc-remote.toml` に table を追加しない。

### 4.4 operator-facing init

TOML projectは次のsurfaceから作る。

```text
mcrctl init <path> --format toml \
  --deployment-name <name> \
  --profile <exact-ref> \
  --environment-identity <identity> \
  --channel <channel> \
  --exposure <exposure> \
  --purpose <purpose> \
  --preset <exact-ref> \
  --artifact-store <absolute-path> \
  --volume <role>=<identity> [...] \
  --world-identity <identity> \
  --bind-address <ipv4> \
  --java-port <port> \
  --mcremote-port <port>
```

- TOMLでは上記instance argumentをすべて必須とし、directory basename、profile、host、
  environment variableからdefaultを補わない。
- `--volume` はrepeatableだがrole重複を拒否し、全profile roleとの一致はresolveでも検証する。
- `agreements.minecraft_eula` は必ずfalse、acknowledgementはfalse / 空理由で初期化する。
- 全argumentとcross-field combinationをmemory上で検証してからproject directoryを作る。
- targetがnon-emptyなら上書きしない。failed initはpartial projectを残さない。
- `.gitignore`、project `README.md`、human-owned `mc-remote.toml`だけを作る。
  placeholder lock、artifact store、runtime volume、world、generated outputは作らない。
- legacy YAML initは移行期間中の別surfaceとして`--format legacy-yaml`（default）に残す。
  TOML-only argumentをlegacy設定値として暗黙利用しない。

## 5. include を持たない

初期 schema に次を追加しない。

- `include`
- `import`
- `extends`
- `mc-remote.d/*.toml`
- file glob
- directory recursion
- `${ENV_VAR}` 等の order 内置換
- parent project / workspace からの暗黙継承

profile と preset は bundled registry 内の exact `name@revision` 参照であり、任意 path の
text include ではない。これにより、共有値の変更は immutable revision の追加と明示 resolve を経る。

host 固有の同じ値を sibling project に書く必要がある場合も、初期実装は明示重複を許容する。
二環境以上で同じ編集が継続的に発生し、owner と validation を型付けできる実績ができた場合だけ、
generic include ではなく profile field、typed workspace、または adapter input として別設計する。

## 6. project discovery

### 6.1 explicit root

`mcrctl` は `--project <directory>` で project root を明示的に受ける。

- parent directory を上向き探索しない。
- child directory を再帰探索しない。
- current working directory から project を暗黙選択しない。
- sibling project を自動で束ねない。
- directory basename を identity として使わない。

`--project` が示す directory 直下の `mc-remote.toml` だけを order として読む。
file path 自体を `--project` に渡す別形式は設けない。

### 6.2 multiple projects

一つの Git repository や operator workspace に sibling project を複数置くことはできる。

```text
deployments/
├─ home-beta/
│  ├─ mc-remote.toml
│  └─ mc-remote.lock.toml
└─ home-alpha/
   ├─ mc-remote.toml
   └─ mc-remote.lock.toml
```

`deployments/` 自体は deployment project でも workspace manifest でもない。
各 command は一つの `--project` だけを対象にする。

同一 host に複数 project を apply する場合は、全対象 lock を入力にした host-level collision
check が実装されるまで二つ目を supported apply path に入れない。最低限、Compose project name、
public / bind port、runtime volume、world path、hostname / route の衝突を検査する。

## 7. `mc-remote.lock.toml`

### 7.1 creation

`mcrctl init` は human-owned `mc-remote.toml` を作るが、unresolved placeholder lock は作らない。
最初の `mcrctl resolve` が全 gate を通過したときだけ `mc-remote.lock.toml` を生成する。

- lock が無い project は正常な unresolved state である。
- `validate` は order 単体を検証できる。
- `plan` は `lock_missing` を示して resolve を要求する。
- `render` / 将来の `apply` は lock 無しで停止する。
- failed resolve は既存 lock を削除・切り詰め・placeholder 化しない。

### 7.2 physical unit

一つの `mc-remote.lock.toml` は一つの EnvironmentLock だけを持つ。
複数 EnvironmentLock の array や environment name を key にした map は作らない。

lock は project の `deployment.name`、environment 4軸、instance-owned contract、order semantic digest、
profile / preset ref と digest を含み、別 project へ copy した場合も order 不一致を検出する。

lock は secret 値を持たないため、通常は order と同じ Git history に commit する。
`mc-remote.lock.toml` を `.gitignore` へ入れない。

### 7.3 update

lock は stable serializer で全体生成し、human comment を保持する編集面にしない。
candidate lock identity が既存 lock と同じなら file を書き換えない。異なる場合だけ temporary file、
flush / fsync、atomic replace で更新する。

## 8. operator-owned auxiliary input

`operator/` は、plugin / adapter が必要とする human-owned native config を order から分離する
予約領域である。全 human-owned input を `mc-remote.toml` 一つへ押し込まない。

F では次の境界を固定した。各 plugin field の owner は後続のplugin config ownership設計で決める。

1. file は `operator/<adapter>/<native-path>` の下に置く。
2. `mc-remote.toml` が adapter ID と相対 path を明示参照する。
3. glob、directory 丸ごと、暗黙 filename discovery を使わない。
4. `..`、absolute path、project 外 symlink を拒否する。
5. unreferenced file は `operator_input_unreferenced` として拒否する。
6. secret material を置かない。secret は reference / injection point だけを記述する。
7. resolver は adapter が作る semantic digest を lock に記録する。
8. renderer は source file を変更せず、必要なら `generated/` へ投影する。

operator input の native format が YAML / JSON / properties でも、それは order format ではない。
adapter は schema、owner、restart / reload requirement、secret injection boundary を宣言するまで
その file を有効化できない。

adapter が native file を semantic model へ parse できる場合、comment、空白、key 順等を除いた
semantic digest だけを lock identity に含める。source bytes をそのまま runtime へ渡す
byte-exact adapter では content SHA-256 自体を semantic digest とし、lexical change も意味のある
変更として扱う。raw bytes hash を provenance のためだけに更新して no-op lock を書き換えない。

### 8.1 typed reference と profile ownership

orderはoptional inputをarray of tablesで明示する。role、adapter、pathはすべてexact valueであり、
同じroleまたはpathを重複できない。

```toml
[[operator_inputs]]
role = "minecraft-motd"
adapter = "minecraft-motd@1"
path = "operator/minecraft-motd/server.properties"
```

選択したprofileも同じroleとadapterを宣言し、requiredかoptionalかを所有する。
`home-server@2` の宣言は次である。既存のimmutable `home-server@1`は変更しない。

```toml
[[operator_input_roles]]
id = "minecraft-motd"
adapter = "minecraft-motd@1"
required = false
```

orderだけで未知roleを追加する、profileと異なるadapterへ差し替える、未対応adapterを選ぶ操作は
fail closedとする。`init` はoptional inputを推測して作らず、必要なoperatorが明示的に追加する。

### 8.2 `minecraft-motd@1`

最初のtyped adapterはMinecraftの公開表示文だけを所有し、plugin設定やsecretの汎用入力ではない。
sourceはUTF-8、BOMなし、最大4096 bytesの限定Java properties形式とする。

```properties
# Public server-list text
motd=McRemote home beta
```

- 空行、`#` / `!` comment、`motd`前後の空白だけをlexical差として無視する。
- `motd`をexactly once要求し、値は1〜256文字とする。
- unknown / duplicate key、escape、continuation、control character、invalid UTF-8を拒否する。
- `secret://` と`${...}`を拒否する。このadapterにsecret injection pointはない。
- lockはrole、adapter、exact path、`{"motd": "<value>"}`のsemantic modelとそのSHA-256を持つ。
- commentや空白だけの変更ではlock identity、lock bytes、mtimeを変えない。
- 値の変更では既存lockをstaleとし、再resolveを要求する。
- rendererはsource bytesをcopyせず、lock済みsemantic valueから`motd=<value>`だけを
  `generated/minecraft/server.properties`へ投影する。

任意文字列に秘密が含まれるかを推測する機能ではない。operatorは公開表示以外の値を書いては
ならず、secretを必要とする後続adapterは別のtyped reference / injection設計を必須とする。

## 9. lossless editing

### 9.1 reader と editor を分ける

- semantic read / validation: Python 3.11+ の `tomllib`
- comment / whitespace / key-order preserving edit: TOML Kit
- machine-owned lock: stable full serializer

order を `tomllib` の `dict` から全体再serializeしてはならない。

TOML Kit は style-preserving parser / editor として comments、indentation、whitespace、
internal ordering を保持する。初期実装は `tomlkit>=0.15.1,<0.16` を dependency range とし、
`uv.lock` が exact version を固定する。resolver は実際に使った tool version を lock に記録する。

- [TOML Kit project description](https://pypi.org/project/tomlkit/)
- [TOML Kit editing quickstart](https://tomlkit.readthedocs.io/en/latest/quickstart/)

TOML Kit が記載する out-of-order array-of-tables sub-table の physical placement normalization を
避けるためにも、order の environment は singular table とし、複数 environment の
array-of-tables を編集面にしない。

### 9.2 controlled update

`accept-eula` 等、`mcrctl` が order を更新する command は次の順序を守る。

1. original bytes を読む。
2. `tomllib` で parse / schema validation する。
3. TOML Kit document model で同じ bytes を parse する。
4. command が owner である exact logical path だけを変更する。
5. TOML Kit で candidate bytes を作る。
6. candidate を `tomllib` で再parseし、schema validation する。
7. original / candidate の semantic tree を比較し、許可 path 以外の変化が無いことを確認する。
8. temporary file、flush / fsync、atomic replace で更新する。

parse error、duplicate key、semantic drift、unsupported TOML Kit layout があれば original file を
変更しない。

### 9.3 preservation contract

限定更新で保持しなければならないもの:

- 対象外 comment
- 対象外 key / table の順序
- 空行
- indentation
- quote style
- inline comment
- array layout

更新対象 scalar 自体の quote style を維持できない場合は、command を失敗させるか、
変更前後を plan に明示する。無関係な全体 format を正規化しない。

## 10. YAML / TOML の同居

project root で次の legacy order / lock name を検出する。

```text
mc-remote.yml
mc-remote.yaml
mc-remote.lock.yml
mc-remote.lock.yaml
```

挙動を次に固定する。

| 状態 | 結果 |
| --- | --- |
| TOML order + legacy YAML のいずれか | `mixed_order_formats` で全 command停止 |
| legacy YAML だけ | `legacy_order_requires_explicit_conversion` で新 resolver停止 |
| TOML lockだけ、TOML orderなし | `orphan_lock` で停止 |
| TOML orderあり、lockなし | unresolved project として validate可 |
| TOML order + TOML lock | digest / identity を検証して続行 |

検出範囲は project root 直下の上記 exact name とする。次は order format の同居ではない。

- `generated/**/*.yaml` / `generated/**/*.yml`
- order から明示参照され、adapter schema が所有する `operator/**` の native YAML
- package 内の test fixture

new resolver に legacy YAML reader、暗黙優先、fallback parse を持たせない。

## 11. explicit conversion

汎用 `mcrctl migrate` は作らない。既知の `official-vps` は一度限りの明示変換 fixture とし、
次の順で比較する。

1. legacy source directory を read-only input として固定する。
2. empty な別 target project に `mc-remote.toml` を作る。
3. existing exact artifact identity から新 lock を resolve する。
4. legacy / TOML の validate、plan、render を比較する。
5. 人間が差分を確認する。
6. TOML project を採用した後に legacy project を live path から外す。

同じ project root に YAML と TOML を並べて段階切替しない。
構文変換だけで lock を作らず、profile / preset / artifact / compatibility evidence を再解決する。

追加 project も、切替前に人間が対象を明示したものだけを個別変換する。

legacy `official-vps` が stable / beta を一つの YAML tree に持つ場合、新 TOML でも一 file に
残さない。比較対象にする environment ごとに sibling target project を作り、legacy 全体の
render output と sibling project 群の render output を対応付けて比較する。

## 12. home deployment 順序

### 12.1 `home-beta`

最初に一つだけ作る。

```text
deployments/home-beta/
├─ mc-remote.toml
└─ mc-remote.lock.toml   # successful resolve後
```

directory name から beta channel を推測せず、order に4軸を明示する。
live apply 前に単独 project の profile / preset / artifact / security gate を通す。
初期operator経路は`init --format toml`、`validate`、`accept-eula`、明示acknowledgement、
`resolve`、`plan`、`artifact fetch`、`render`の順とする。現段階ではrender後に停止し、
Composeを起動しない。

### 12.2 `home-alpha`

`home-beta` の live path が確立した後、別 project として追加する。

```text
deployments/home-alpha/
├─ mc-remote.toml
└─ mc-remote.lock.toml
```

`home-beta` から directory copy して identity や lock を流用しない。
`mcrctl init` から新しい order を作り、exact profile / preset を選び直す。
別 runtime volume・別 world identity を必須とし、同一 host の二つ目として apply する前に
host-level collision check を通す。

### 12.3 `official-vps`

当面は live deployment project として扱わず、legacy YAML と新 TOML の明示変換、
validate / plan / render comparison の deterministic regression fixture とする。
stable / beta の両方を fixture に含める場合も、一 environment 一 sibling project とする。

## 13. test-first implementation gate

コード着手時は、少なくとも次の失敗 test を先に追加する。

### project discovery

- explicit project root 直下の exact order name だけを読む
- parent search / child recursion / cwd implicit selection をしない
- directory basename から identity / channel を導かない
- sibling project を一 command で暗黙処理しない
- TOML initが全instance argument必須で、directory名から値を補わない
- invalid argument / cross-field combinationでpartial projectを残さない

### physical grain

- singular `[environment]` を受理する
- `[[environments]]` と複数 environment を拒否する
- project root 内の追加 order file を拒否する
- `home-alpha` 追加で `home-beta` order / lock bytes が変わらない
- one lock が one EnvironmentLock だけを持つ

### include

- `include` / `import` / `extends` / glob / environment interpolation を拒否する
- profile / preset exact ref だけを reusable input として解決する
- unreferenced `operator/**` を拒否する
- operator path traversal / project 外 symlink を拒否する

### operator input lifecycle

- orderのrole / adapterが選択profileの宣言と一致しなければ拒否する
- invalid UTF-8、unknown / duplicate key、continuation、secret referenceを拒否する
- comment / 空白だけの変更でlock identityとlock fileを変更しない
- semantic value変更で既存lockをstaleとする
- rendererがsourceを再読込せず、lock済みsemantic valueだけを投影する

### lossless edit

- no-op edit が order bytes / mtime を変えない
- targeted scalar edit が対象外 comment / whitespace / order / quote style を保つ
- `tomllib` semantic tree の差分が allowlist path だけになる
- parse / validation / preservation failure で original bytes を保つ
- temporary write / atomic replace の failure で partial file を残さない

### lock lifecycle

- init が placeholder lock を作らない
- successful resolve だけが lock を作る
- failed resolve が既存 lock を変更しない
- lock が通常の `.gitignore` 対象でない
- copied lock と別 order の digest mismatch を拒否する

### artifact lifecycle

- initはartifact storeを作らない
- fetchはcurrent lockのartifact storeだけを使い、CLI overrideを受けない
- existing content-addressed entryを再hashし、tamper時に上書きしない
- failed fetchでpartial destinationを残さない

### render lifecycle

- missing / stale / tampered lock でoutputを変更しない
- content-addressed artifactの欠落 / digest不一致でoutputを変更しない
- 同一renderがgenerated bytes / mtimeを変更しない
- unmanaged / tampered outputを上書きしない
- managed replacementのpublish失敗で旧outputを保持する
- render outputとproject / artifact storeのoverlapを拒否する

### mixed formats

- TOML + exact legacy YAML name を `mixed_order_formats` で拒否する
- legacy YAML only を explicit conversion 要求で拒否する
- generated YAML を mixed order と誤判定しない
- explicitly owned operator YAML を mixed order と誤判定しない
- TOML lock only を `orphan_lock` で拒否する

### rollout

- `home-beta` fixture が単一 environment project である
- `home-alpha` fixture が sibling project である
- 二つの runtime volume / world identity が一致しない
- `official-vps` が live apply test の対象にならない

## 14. F の完了条件

F は次を実装とtestで満たし、完了した。

- 一 environment 一 project 一 order 一 lock が強制される
- generic include / implicit discovery が存在しない
- `mc-remote.toml` の lossless targeted edit が検証される
- owner surface が path 単位で分かれる
- referenced operator input の adapter semantic digest が lock identity に入る
- placeholder lock を作らず、successful resolve だけが lock を生成する
- YAML / TOML の同居が定義した範囲で fail closed になる
- `home-beta` と `home-alpha` が独立 project / volume / world になる

次の設計は、F の layout を変えずに plugin config ownership と host-level multi-project
collision / upgrade transaction を閉じる。初回bootstrap applyの詳細は
[`home-beta` bootstrap apply設計](home-beta-bootstrap-apply-design_ja.md)を正とする。
generic include や multi-environment order へ戻さない。
