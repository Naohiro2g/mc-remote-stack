# Scratch–Stack deployment interface 実装境界

この文書はknowledge `2026-08-31-01`のStack投影である。横断contractの正本ではない。

## 通常経路

operatorが編集する入力は`mc-remote.toml`一つである。`apply <mc-remote.toml>`はpreset解決、exact lock、
Scratch runtime configとBridge allowlistの共通target集合からの生成、Scratch schema validation、artifact取得、
render、Docker preflight、create／update判定、起動を進める。`doctor <deployment>`は配信runtime configを
lock済みScratch schemaへ通し、実containerのBridge allowlistが同じtarget集合と一致することを確認する。

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

Scratch commit `689fd1edc5e123a59a633bbf6528ba18879e39dd`、runtime-config tree
`ecb669a02ac6c8e502b44850e6dd28260c5adad4`のschema／fixtureは収容済みである。ただしScratch担当からの正式な
image digest handoffは未受領なので、このcontractを使うbundled `classroom@1`はまだ登録しない。Stack担当が
起動したimage workflowの出力は正式artifactに採らず、presetやlockから参照しない。

Scratch担当からimage digestを受領した後は、そのdigestをScratch artifactと
`deployment_interface.scratch_contract.image_digest`の双方へ固定した新しいimmutable presetを追加する。
StackはScratch／Bridge／Plugin imageをbuildせず、digest一致の検証とlockだけを行う。

product-config contractとcontract directory外のScratch sourceは収容・参照しない。

## 旧経路との境界

既存の`--project`、`--bootstrap`、`deployment update`は移行対象deploymentのために残すが、新しい通常経路の
operator操作には露出しない。探索版`home-server@7`／`compose@15`は取り込まない。runtime configへ
`release_identity`を生成せず、Bridge allowlistへtarget集合外のhostnameを追加しない。
