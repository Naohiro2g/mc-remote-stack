# 通常dev環境 runbook

このrunbookは、開発者workstationから別のserver host上にある一つのMcRemote dev runtimeを利用する
ための最短正準経路を示す。物理host名、SSH alias、private address、secret実値はこの文書へ固定しない。
対象hostとの対応はprivate inventory、論理deploymentはStackのorder／lockで管理する。

knowledge contractは`2026-08-21-03`／`2026-08-21-04`。b5 gateのexact setが未凍結である間、
**candidate deployは未許可**である。この状態ではhostのread-only preflightとorder入力の準備までで停止する。

## 1. topologyとidentity

通常dev環境の論理identityは次とする。

- deployment／environment: `dev-integration`
- channel: `dev`
- purpose: `integration`
- exposure: `lan-only`
- profile: `home-server@5`
- server側候補port: Minecraft Java `25566`、McRemote `25576`

`home-server@5`は`home-server@3`のserver側topologyを再利用するappend-only revisionである。
Compose serviceはMinecraft／Paper／McRemoteだけとし、world、credential store、revocation authorityを
別volumeにする。rendererは同じ`compose@5`を使用する。一方、通常devとして`dev` channelだけを許可し、
認証強制とsession-only policyをprofile capabilityへ明示する。

既存profileをそのまま使わない理由は名前やchannelの印象ではない。immutableな`home-server@3`は
`dev`を許可せず、認証強制／session-onlyをcapabilityとして宣言していないためである。public web edgeを
持つ`vps-server`系は、Caddy、Scratch、Bridge、public route、TLSをserver側へ要求し、このtopologyと一致しない。

```text
開発者workstation                         server host
Minecraft client ─── LAN/TCP 25566 ───> Minecraft/Paper
Scratch browser ──> local Bridge ──────> McRemote TCP 25576
Python process ─────────────────────────> McRemote TCP 25576
WireScope browser <── workstation内のMessageChannel／loopback station
```

GUI、browser、Minecraft Launcherをserver hostへ導入しない。Scratch、Bridge、Python、WireScopeは
開発者workstation側で実行する。server hostへ配備するartifactはOCI runtime、Paper JAR、McRemote JARだけである。

## 2. host preflight

対象hostでは、信頼された非root operatorとしてStackをcheckoutした後、次の一入口を使う。

```sh
cd "$HOME/mc-remote-stack"
tools/bootstrap-ubuntu-operator.sh --check
```

不足がある場合だけ、人間operatorが同じscriptのinstall modeを実行する。

```sh
tools/bootstrap-ubuntu-operator.sh --install
```

installはGit、固定uv、Python、Docker Engine、Docker Compose、repo virtual environmentを準備する。
group変更を報告した場合は再ログインし、`--check`を再実行する。project作成後は次でもowner、mode、
artifact store、local Docker contextをまとめて検査する。

```sh
"$HOME/mc-remote-stack/.venv/bin/mcrctl" operator check \
  --project "$MC_REMOTE_PROJECT" \
  --docker-context default \
  --bootstrap-ports
```

server側の予定portにlistenerがあれば、既存runtimeを停止・上書きせず別portを選ぶ。LAN bindの実値、
host／network firewall、開発者workstationからの到達性は人間checkpointである。addressをhost名から推測しない。

## 3. exact set受領前の停止点

次の値はgate coordinatorがexact setを凍結するまで設定しない。

```sh
EXACT_PRESET_REF=""  # exact set未凍結中は設定しない
```

必要な入力は次のとおり。

- knowledge contract commitとauthorized next action
- exact preset refとpreset semantic digest
- protocol、Minecraft、Paper、Java floor
- McRemoteのpush済みsource commit、artifact名、version、bytes、SHA-256、credential-free HTTPS取得元
- Scratch／Bridgeのpush済みcommit、CI run／artifact ID／digest、展開後inventory
- Python wheel／sdistのversion、bytes、SHA-256、Python floor
- WireScope ZIP／detached manifestのbytes、SHA-256、schema／handoff version
- queue、ring、poll、handle、particle、work、timeoutの確定runtime policy値とfixture identity

exact presetとbootstrap tupleのreview入力枠は
[`examples/normal-dev-exact-preset.template.toml`](../examples/normal-dev-exact-preset.template.toml)
に置く。これはresolverが読むbundled presetではなく、placeholderを残したまま使用できないreview checklistである。
exact set受領後の別PRで、全placeholderをreview済み値へ置換したappend-only presetをregistryへ追加し、catalogを
再生成する。同じPRで、template冒頭に示すexact 5-tupleだけを`BOOTSTRAP_CONTRACTS`へappendする。

`artifact fetch`はlockにあるcredential-free HTTPS fileを取得する。GitHub Releaseを先行作成せずに固定する
McRemote JARは`kind = "git-build"`としてsource／build provenanceとoutput SHA-256をpreset／lockへ固定し、
coordinatorがreviewした同一bytesだけを`mcrctl artifact import-reviewed`でCASへ入れる。取得時の一時URLや認証は
lock identityにせず、import後の`<artifact_store>/sha256/<output_sha256>`をdeploy入力とする。期限付きCI artifactを
恒久originとして参照しない。未push commit、担当worktreeの`build/libs`、`/tmp`、movingな「latest」は入力にしない。

Scratch／Bridge、Python、WireScopeのexact identityは横断compatibility setに必要だが、このserver-only profileで
hostへ配備するartifactではない。b5のために未使用artifactやworkstation runtimeをserver presetへ混入せず、
coordinatorの統一実施票と各componentのdistribution metadataで照合する。

profile追加だけでは初回applyを許可しない。exact presetを登録する変更で、`BOOTSTRAP_CONTRACTS`へ
`home-server@5`、そのexact preset、`dev`、選択したexposure、`integration`の組をappendする。
それまではresolve後のlockが存在してもapplyを`bootstrap_contract_unsupported`でfail closedにする。

## 4. order／lockの骨格

exact setとLAN bindが批准された後、手書きTOMLではなく`init`で一environment一projectを作る。

```sh
MC_REMOTE_STACK="$HOME/mc-remote-stack"
MC_REMOTE_PROJECT="$HOME/mc-remote-deployments/dev-integration"
MC_REMOTE_ARTIFACT_STORE="$HOME/.local/share/mc-remote/artifacts"
EXACT_PRESET_REF="<coordinatorが凍結したpreset@revision>"
REVIEWED_DEV_BIND_ADDRESS="<private inventoryで確認したLAN bind address>"

"$MC_REMOTE_STACK/.venv/bin/mcrctl" init "$MC_REMOTE_PROJECT" \
  --format toml \
  --deployment-name dev-integration \
  --profile home-server@5 \
  --environment-identity dev-integration \
  --channel dev \
  --exposure lan-only \
  --purpose integration \
  --preset "$EXACT_PRESET_REF" \
  --artifact-store "$MC_REMOTE_ARTIFACT_STORE" \
  --volume minecraft-data=dev-integration-minecraft-data \
  --volume credential-store=dev-integration-credential-store \
  --volume credential-revocations=dev-integration-credential-revocations \
  --world-identity dev-integration-world \
  --bind-address "$REVIEWED_DEV_BIND_ADDRESS" \
  --java-port 25566 \
  --mcremote-port 25576
```

orderは論理identity、profile／preset ref、三volume、world、bind、port、EULA acknowledgementを持つ。
lockは`resolve`だけが生成し、profile／preset semantic digest、component／artifact identity、render plan、
instance値、compatibility statusを固定する。lockを手編集しない。

## 5. 初回applyの正準入口

この節はcoordinatorがexact setとcandidate deployを許可した後だけ実行する。
正準操作入口は`mcrctl operator check` → `mcrctl validate` → `mcrctl accept-eula` →
`mcrctl resolve` → `mcrctl plan` → `mcrctl artifact fetch` → `mcrctl artifact import-reviewed` → `mcrctl render` →
`mcrctl apply` → `mcrctl doctor`の順である。

```sh
MCRCTL="$MC_REMOTE_STACK/.venv/bin/mcrctl"

"$MCRCTL" operator check \
  --project "$MC_REMOTE_PROJECT" \
  --docker-context default \
  --bootstrap-ports
"$MCRCTL" validate --project "$MC_REMOTE_PROJECT"
"$MCRCTL" accept-eula --project "$MC_REMOTE_PROJECT" --yes
"$MCRCTL" resolve --project "$MC_REMOTE_PROJECT" --allow-unverified
"$MCRCTL" plan --project "$MC_REMOTE_PROJECT"
"$MCRCTL" artifact fetch --project "$MC_REMOTE_PROJECT"
"$MCRCTL" artifact import-reviewed "$REVIEWED_MCREMOTE_JAR" \
  --project "$MC_REMOTE_PROJECT" \
  --artifact-id mcremote-jar \
  --expected-sha256 "$REVIEWED_MCREMOTE_SHA256"
"$MCRCTL" render \
  --project "$MC_REMOTE_PROJECT" \
  --output "$MC_REMOTE_PROJECT/generated"
```

`REVIEWED_MCREMOTE_JAR`と`REVIEWED_MCREMOTE_SHA256`は、coordinatorがexact setで指名したfileとdigestだけを
設定する。importはcurrentかつself-verifyingなlockの`git-build` artifact一件に限定し、filenameとdigestを再検査する。
既存CAS entryが同digestなら再hashして`present`、不一致なら上書きせず停止する。source fileはsymlinkを拒否し、
同一filesystem内のtemporary fileからcreate-if-absentでpublishする。この操作はbuild、download、render、applyを行わない。

`plan`で表示したlock identity、artifact、三volume、world、LAN bind／portを人間が確認する。HTTPS artifact取得、
review済みgit-build outputのimport、renderが成功するまでruntimeを変更しない。承認したlockだけを初回bootstrapする。

```sh
REVIEWED_LOCK_IDENTITY="sha256:<planで確認した64-hex>"

"$MCRCTL" apply \
  --project "$MC_REMOTE_PROJECT" \
  --output "$MC_REMOTE_PROJECT/generated" \
  --expected-lock-identity "$REVIEWED_LOCK_IDENTITY" \
  --docker-context default \
  --bootstrap \
  --yes \
  --allow-unverified

"$MCRCTL" doctor --project "$MC_REMOTE_PROJECT" --docker-context default
```

成功条件はcurrent lock／render、managed service／volume、exact artifact mount、LAN bind／port、healthy、
token無しhelloの`auth_required`である。doctorがFAILした場合、その場でgenerated fileやcontainerを手修正せず、
reasonを保持してStackまたはcomponent担当へ戻す。

## 6. candidate更新と再適用

稼働後のcandidate更新は、同じprofile／preset familyのreview済みexact targetに対して二commandを使う。
入口は`mcrctl deployment update plan`と`mcrctl deployment update apply`であり、release固有migrationを
追加しない。

```sh
"$MCRCTL" deployment update plan \
  --project "$MC_REMOTE_PROJECT" \
  --to-profile home-server@5 \
  --to-preset "$NEXT_EXACT_PRESET_REF" \
  --docker-context default

"$MCRCTL" deployment update apply \
  --project "$MC_REMOTE_PROJECT" \
  --plan-id "$REVIEWED_UPDATE_PLAN_ID" \
  --yes
```

planはtarget artifactを停止前に取得・再hashし、source／target order、lock、render、volume、portを一票にする。
applyはreview済みplan IDだけを使い、失敗時は旧order／lock／renderとcontainerを再起動できる範囲で戻す。
world、pairing、session、接続状態の完全復元は主張しない。Scratch／Pythonの保存済み建築コードを保護対象とする。

同じplanが途中で停止した場合は、ad hoc commandを挟まず同じplan IDで`apply`を再実行する。別candidateへ
変更する場合はcoordinatorが旧exact setを失効させた後、新しいtargetでplanを作り直す。汎用destroyや
完全rollbackをこのrunbookの前提にしない。
