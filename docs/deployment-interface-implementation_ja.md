# Scratch–Stack deployment interface 実装境界

この文書はknowledge `2026-08-31-01`のStack投影である。横断contractの正本ではない。

## 通常経路

`classroom@1`はScratch／Bridge／Minecraftの3サービス、`home-alpha-full@2`は同じ設定生成・contract検査へCaddyのloopback HTTP転送を加えた4サービスである。`home-alpha-full@3`は同じ4サービスでWireScopeを別HTTP portから静的配信し、ZIP／manifest照合とhandoff headerを通常apply／doctorへ加える。ホーム用のHTTPS／WSSはhost側の転送が終端する。ホーム用の起動前確認は[`ホーム用ケータリング手順`](home-catering-guide_ja.md)を参照する。

`apply <mc-remote.toml> --dry-run --output <empty-directory>`はorderからexact lock、Compose、runtime設定を生成する。artifact取得、Docker操作、current deployment stateの更新は行わない。portの空きやHTTPS入口の到達性はこの操作では未確認であり、hostの観測と起動後のdoctorで確認する。通常applyは進行段階を表示し、Docker command失敗時はexit statusとstderr末尾を返す。

operatorが編集する入力は`mc-remote.toml`一つである。`apply <mc-remote.toml>`はpreset解決、exact lock、Scratch runtime configとBridge allowlistの共通target集合からの生成、Scratch schema validation、artifact取得、render、Docker preflight、create／update判定、起動を進める。`doctor <deployment>`は配信runtime configをlock済みScratch schemaへ通し、exact image、container稼働／Minecraft health、公開port、実containerのBridge allowlist／default target、Bridge containerからMcRemoteへの到達、tokenなし`hello`への`auth_required`を確認する。targetの`sandbox`は同じtarget集合からMinecraft serviceの内部network aliasにも生成し、Bridgeが同じdeploymentのMcRemoteへ接続する経路を固定する。McRemoteの非秘密runtime policyは初回seed用configとして生成し、b7 JARのfresh-install既定に依存せず`auth.enforcement: true`を設定する。実際のenforcementはconfig内容の推測でなくdoctorのtokenなし`hello`で確認する。credential実値は生成せず、session-only store／authorityはMinecraft data volume内のruntime stateとして扱う。

`apply`はcurrent exact lock、永続world volume、管理containerの三者から状態を判定する。lockとvolumeが揃う既存deploymentはcontainerが停止／削除済みでもupdateであり、新規扱いにしない。既存world volumeを再利用する場合だけpreset family変更とrevision後退をmigration要求として停止する。新しいdeployment／volumeにはrevisionの大小を持ち込まない。必要host portはartifact取得とrender公開より前に確認し、同deploymentが現在所有するportだけをupdate時の占有から除外する。

生成したlockとrenderはoperator入力ではなく、既定では`$XDG_STATE_HOME/mc-remote/deployments/<deployment>/`へ置く。`MC_REMOTE_STATE_HOME`を指定した場合は、そのdirectoryの`deployments/`以下を使う。artifact storeは既存のcontent-addressed cache契約を使う。

WireScope asset、Scratch runtime JSONとCaddy設定はcontainerから読める`0644`、lock／current等は`0600`で保存する。同じ内容のファイルはinodeを維持し、権限だけを補正できるため、既存のbind mountにも補正が反映される。applyはCompose起動後の`verify`で管理container集合、image、稼働、port、Minecraft healthを再取得して確認し、成功してからcurrentを記録する。applyとdoctorは`Running=true`でも`Restarting=true`なら`deployment_runtime_restarting`で停止する。実際の配信設定・接続先・認証強制の確認はdoctorで行う。WireScopeを含むpresetではpublic indexのSHAとScratch／WireScope両originのresponse headerも検査する。認証データ初期化の状態はMcRemoteのcredential statusで別に確認する。

## Scratch contract handoffの取込

Stackへの正式入力は、Scratch GitHub Releaseへ添付された`manifest.json`と`contracts.tar.gz`である。manifestがrole別に確定するsource commit、Scratch／Bridge image digestを個別のテキスト票として会話やhandoffから聞き出す運用はしない（`00-hub/deployment-interface-design_ja.md`§4）。

Stack担当は取得したruntime-config contract directoryを`src/mc_remote_stack/data/scratch-contracts/<commit>/`へそのまま収容する。immutable presetはcommit、Git tree SHA、schema SHA-256、全fixtureのSHA-256とaccept／reject期待値、source directory、mount path、Scratch image digestを固定する。resolveとdoctorは収容したdirectoryのGit tree identityを再計算し、schema／fixture digestと全fixtureの判定を再実行する。presetのScratch artifact digestとhandoff image digestが異なる場合も停止する。

実際のcommit／tree／digestはpreset revisionごとに`src/mc_remote_stack/data/preset_registry/`配下のexact preset.tomlが持つ。この文書はそれらの値を転記しない。Scratch GitHub Releaseに`manifest.json`が添付されている場合、Stackはそのmanifestのrole別artifact identity（source commit、Scratch／Bridge image digest）だけを正式入力とし、tagとregistry manifestをread-only照合する。imageはbuildしない。前回Stackが起動したworkflowのimage digestはpresetやlockから参照しない。

Scratch digestはScratch artifactと`deployment_interface.scratch_contract.image_digest`の双方へ同じ値を固定し、Bridge digestもexact artifactとして固定する。resolve時にScratch artifactとhandoff digestが異なれば停止する。

contract directory外のScratch sourceは収容・参照しない。

## 旧経路との境界

既存の`--project`、`--bootstrap`、`deployment update`は移行対象deploymentのために残すが、新しい通常経路のoperator操作には露出しない。探索版`home-server@7`／`compose@15`は取り込まない。runtime configへ`release_identity`を生成せず、Bridge allowlistへtarget集合外のhostnameを追加しない。
