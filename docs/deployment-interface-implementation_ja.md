# Scratch–Stack deployment interface 実装境界

この文書はknowledge `2026-08-31-01`のStack投影である。横断contractの正本ではない。

## 通常経路

operatorが編集する入力は`mc-remote.toml`一つである。`apply <mc-remote.toml>`はpreset解決、exact lock、
Scratch runtime configとBridge allowlistの共通target集合からの生成、Scratch schema validation、artifact取得、
render、Docker preflight、create／update判定、起動を進める。`doctor <deployment>`は配信runtime configを
lock済みScratch schemaへ通し、exact image、container稼働／Minecraft health、公開port、実containerのBridge
allowlist／default target、Bridge containerからMcRemoteへの到達、tokenなし`hello`への`auth_required`を確認する。
targetの`sandbox`は同じtarget集合からMinecraft serviceの内部network aliasにも生成し、Bridgeが同じdeploymentの
McRemoteへ接続する経路を固定する。
McRemoteの非秘密runtime policyは初回seed用configとして生成し、b7 JARのfresh-install既定に依存せず
`auth.enforcement: true`を設定する。実際のenforcementはconfig内容の推測でなくdoctorのtokenなし`hello`で確認する。
credential実値は生成せず、session-only store／authorityはMinecraft data volume内のruntime stateとして扱う。

`apply`はcurrent exact lock、永続world volume、管理containerの三者から状態を判定する。lockとvolumeが揃う
既存deploymentはcontainerが停止／削除済みでもupdateであり、新規扱いにしない。既存world volumeを再利用する
場合だけpreset family変更とrevision後退をmigration要求として停止する。新しいdeployment／volumeにはrevisionの
大小を持ち込まない。必要host portはartifact取得とrender公開より前に確認し、同deploymentが現在所有するportだけを
update時の占有から除外する。

生成したlockとrenderはoperator入力ではなく、既定では
`$XDG_STATE_HOME/mc-remote/deployments/<deployment>/`へ置く。`MC_REMOTE_STATE_HOME`を指定した場合は、
そのdirectoryの`deployments/`以下を使う。artifact storeは既存のcontent-addressed cache契約を使う。

## Scratch contract handoffの取込

Stackへ返す正式入力は次の五点である。

- Scratch contract commit
- `packages/scratch-gui/contracts/runtime-config` directory
- container mount path
- Scratch image digest
- Scratch側で実行したtestと結果

Stack担当は受領済みのruntime-config contract directoryを
`src/mc_remote_stack/data/scratch-contracts/<commit>/`へそのまま収容する。immutable presetはcommit、Git tree
SHA、schema SHA-256、全fixtureのSHA-256とaccept／reject期待値、source directory、mount path、Scratch image
digestを固定する。resolveとdoctorは収容したdirectoryのGit tree identityを再計算し、schema／fixture digestと
全fixtureの判定を再実行する。presetのScratch artifact digestとhandoff image digestが異なる場合も停止する。

正式handoff 2はScratch commit `4c893bd532002d9216665c5c9b9825e09ede1e7c`、runtime-config tree
`ecb669a02ac6c8e502b44850e6dd28260c5adad4`である。Scratch自身のCIがbuild・publishしたScratch／Bridge OCI
index digestをbundled `classroom@1`へ固定した。Stackはtagとregistry manifestをread-only照合し、imageをbuild
しない。前回Stackが起動したworkflowのimage digestはpresetやlockから参照しない。

Scratch digestはScratch artifactと`deployment_interface.scratch_contract.image_digest`の双方へ同じ値を固定し、
Bridge digestもexact artifactとして固定する。resolve時にScratch artifactとhandoff digestが異なれば停止する。

product-config contract（handoff 2 tree `5980d6245da82a26325d415398dafd23e59d8c29`）とcontract directory外のScratch
sourceは収容・参照しない。

## 旧経路との境界

既存の`--project`、`--bootstrap`、`deployment update`は移行対象deploymentのために残すが、新しい通常経路の
operator操作には露出しない。探索版`home-server@7`／`compose@15`は取り込まない。runtime configへ
`release_identity`を生成せず、Bridge allowlistへtarget集合外のhostnameを追加しない。
