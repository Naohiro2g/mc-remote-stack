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

受領後、Stack担当はhandoffの`schema.json`だけを
`src/mc_remote_stack/data/scratch-contracts/<commit>/schema.json`へ収容し、そのSHA-256、source directory、
mount path、image digestを新しいimmutable preset revisionへ固定する。product configやScratch sourceは収容・
参照しない。presetのScratch artifact digestとhandoff image digestが異なる場合、resolve時点で停止する。

handoff未受領の現在は、実imageを指すbundled deployment-interface presetを作らない。test fixture内のschemaと
presetだけで、取込境界、unknown field拒否、exact target／allowlist、create／update判定を決定論的に検証する。

## 旧経路との境界

既存の`--project`、`--bootstrap`、`deployment update`は移行対象deploymentのために残すが、新しい通常経路の
operator操作には露出しない。探索版`home-server@7`／`compose@15`は取り込まない。runtime configへ
`release_identity`を生成せず、Bridge allowlistへtarget集合外のhostnameを追加しない。
