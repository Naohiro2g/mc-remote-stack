# Agent-assisted bootstrap guide

実利用者がMcRemote serverを初期構築するとき、terminalを操作できるagentの支援を最初から
利用できるようにする。agentが全作業を奪うのではなく、反復可能な観測・生成・検証を担当し、
利用者は対象、期待、差分、承認、自己判定を握る。

このguideは特定のLLM製品や、クリーンインストール済みOSを前提にしない。agentの採否や配置は
model名で決めず、そのsessionで観測した権限境界・実行能力境界・検証能力境界で決める。

設計根拠:

- knowledge `20-教材/ai-learning-design_ja.md`: 生成は任せ、判断・検証・統合・言語化は人間が握る
- knowledge `00-hub/llm-agent-boundary-guide_ja.md`: 主体 × 作業 × 現在地 × toolで境界を観測する
- [`fresh host bootstrap guide`](fresh-host-bootstrap-guide_ja.md): 実際のhost手順

## 1. 基準経路と支援の配置

公開runbookの基準経路は、**対象host上へagentをinstallしなくても、人間のterminal操作だけで
完走できること**とする。会話型AIへsanitized出力を返して助言を受けることはできるが、
commandを実行する主体と承認する主体は人間のままである。

terminalを操作するagentは必須要件ではなく、次の支援経路を追加できる。

| 経路 | 位置づけ | agentを動かす場所 | 主な注意 |
| --- | --- | --- | --- |
| human-run | 基準・必須 | agentはterminalを持たない。利用者が対象hostで実行 | 転記ミスを検証し、human-observedと記録する |
| 管理端末 + SSH | 支援経路 | 利用者の管理PCにあるrepo workspace | SSH先と実行userを毎回確認し、remote mutationにも個別承認を置く |
| 対象host上 | 調査中の実験経路 | 専用の非特権OS user session | 既存の個人管理者userでは起動せず、後述のsecurity gateを満たす |

管理端末 + SSHも、それ自体がremote host上の最小権限を保証するわけではない。SSH commandを
許可した後のremote操作はSSH userの権限で実行されるため、local agentのworkspace境界だけに
依存せず、remote側のuser、Docker、sudo、秘密情報の境界を確認する。

対象host上agentは、quotingやlocal filesystem観測では有利になり得る。しかし、agentの認証情報を
serverへ置き、host上の権限を直接扱う追加リスクがあるため、管理端末 + SSHと同格の正規モードには
しない。利用しなくても構築・診断・復旧できるrunbookを先に保つ。

どの経路でも、実hostをprivate forkや公式専用toolで構築しない。同じ公開repo、
profile / preset / order / lock / `mcrctl`を使う。

terminal実行toolを持たない会話型AIでも、利用者がcommandを実行してsanitized出力を返す形で支援できる。
ただし、そのAIは実行したとも検証したとも主張せず、human-run / human-observedとして区別する。

## 2. `mcrctl`がPATHに入る前から支援する

agent支援は`mcrctl`のglobal install後に始まるものではない。agentは次を順に支援できる。

1. 対象host、SSH user、既存管理経路を人間と確認する。
2. OS、Python、Git、uv、Docker / Compose、disk等をread-onlyで棚卸しする。
3. 足りないtoolだけの導入案を提示し、host mutation前に人間の承認を得る。
4. repoをcloneし、`AGENTS.md`と最新knowledge runtime protocolを読む。
5. checkout内で`uv sync --extra dev`、test、Ruffを実行する。
6. checkoutの`.venv/bin/mcrctl`またはrepo rootで`uv run mcrctl`を使う。

bootstrap期は`mcrctl`をuserの`PATH`へinstall済みと仮定しない。ログイン直後に既存checkoutを
使う場合は、位置を確認して次のようにexact pathを使う。

```bash
~/mc-remote-stack/.venv/bin/mcrctl --help
```

恒久的な`uv tool install`、symlink、shell profile変更は別のoperator install契約である。
agentが利便性だけを理由に黙って追加しない。

対象host上でdev agentを実験する場合も、`AGENTS.md`が指定するpublic knowledge取得が必要である。
ただし、個人用GitHub credentialをserverへ常設して解決しない。public SSOTを取得するための
短命・最小scopeの経路を用意できなければ、McRemote固有判断を推測せず、human-runまたは
knowledgeを読める管理端末側agentへ切り替える。

## 3. 最初に渡すprompt

human-runで会話型AIの支援を受ける場合:

```text
McRemote serverの初期構築手順を案内してください。
commandは私が対象hostのterminalで実行し、sanitizedした出力を返します。
実行していないcommandを実行済みと扱わず、各stepの期待値、確認行、停止条件を示してください。
既存OSや無関係なpackageを削除・再インストールしないでください。
EULA、unverified理由、plan/lock review、apply、pairing、
PATH/global tool installは私自身が判断します。
秘密値・private host情報・raw logを公開repoへ保存しないでください。
```

管理端末からSSHする場合:

```text
McRemote serverの初期構築を支援してください。
対象hostとSSH userは私が指定します。まずread-only preflightから始め、
既存OSや無関係なpackageを削除・再インストールしないでください。
mc-remote-stackをclone済みならAGENTS.mdを先に読み、未cloneなら公式repoを取得後に読んでください。
SSH hardening前の別session確認、EULA、unverified理由、plan/lock review、
apply、pairing、PATH/global tool installでは私の明示確認を求めてください。
秘密値・private host情報・raw logを公開repoへ保存しないでください。
各節目で、期待したこと、観測したこと、PASS/未確認、次の一手を短く残してください。
```

SSOTを取得できないagentは、McRemote固有の値をこのrepoだけから推測せず停止する。接続を直すか、
SSOTを読める別のagent surfaceへ作業を移す。securityを弱めるfallbackは作らない。

## 4. 対象host上agentのsecurity評価

この節は利用を推奨するinstall手順ではなく、利用者が同様の方法を試す場合の実験境界である。

### 4.1 確認済みのrisk

- Codex CLIのlocal実行は、sandbox modeとapproval policyの二層で制御される。既定の
  `workspace-write`は書込みをworkspaceへ制限し、commandのnetwork accessを無効にするが、
  `danger-full-access`はsandbox制限を除去する。approval省略はsandbox自体を除去しないが、
  境界外操作の人間確認をなくす。Linuxでは`bubblewrap`等のOS機構が必要である。
- Codexのlogin情報はOS credential storeまたは`~/.codex/auth.json`へ保存される。
  file storageの場合、公式文書も`auth.json`をpassword同様に扱うよう求めている。
- rootful Dockerの`docker` groupはroot-level privilegesを与える。agent userへこのgroupを
  与えてDocker socketを操作させることは、単なるcontainer操作権限の付与ではない。
- SSH agent forwardingを有効にすると、remote host上でsocketへ到達できる主体が、秘密鍵本体を
  取得せずとも、その鍵による認証操作を実行できる。
- GitHub CLIはcredential storeが使えないとtokenを平文fileへfallbackできる。
  `gh auth logout`はlocal保存を消すだけでtokenをrevokeしない。

根拠:

- [OpenAI: Agent approvals & security](https://learn.chatgpt.com/docs/agent-approvals-security)
- [OpenAI: Sandbox](https://learn.chatgpt.com/docs/sandboxing)
- [OpenAI: Authentication](https://learn.chatgpt.com/docs/auth)
- [Docker: Linux post-installation steps](https://docs.docker.com/engine/install/linux-postinstall/)
- [Docker: Docker Engine security](https://docs.docker.com/engine/security/)
- [Docker: Rootless mode](https://docs.docker.com/engine/security/rootless/)
- [OpenSSH: `ForwardAgent`](https://man.openbsd.org/ssh_config.5#ForwardAgent)
- [GitHub CLI: `gh auth login`](https://cli.github.com/manual/gh_auth_login)
- [GitHub CLI: `gh auth logout`](https://cli.github.com/manual/gh_auth_logout)

### 4.2 最初の安全側実験profile

最初の実験は、重要なworldやcredentialを持たない、再構築可能なhostで行う。既存の個人管理者userへ
agentをinstallせず、次をすべて満たす専用userを使う。

- `sudo`、`docker`、`adm`等の管理groupへ所属させず、passwordless sudoを与えない。
- SSH private keyを置かず、`ForwardAgent no`を維持する。
- 個人用GitHub token、`GH_TOKEN`、provider credential、McRemote secretを置かない。
- 専用userが所有するscratch repoだけをagent workspaceとし、別のhome dataをworkspaceへ追加しない。
  個人管理者がapplyに使うcheckout、`.venv`、deployment projectは別に所有し、agent userから
  書込み不可にする。
- 個人管理者はagentが書き込めるcheckoutのscript、`.venv/bin/mcrctl`、generated Composeを
  Docker / sudo権限で実行しない。提案された差分はreviewし、管理者側のtrusted checkoutへ
  人間が反映してから実行する。
- Linux sandboxの起動warningがないことと、`/status`でworkspaceを人間が確認する。
- 最初は次のようにread-only、human approval reviewerで開始する。

```bash
codex --sandbox read-only --ask-for-approval on-request \
  -c 'approvals_reviewer="user"'
```

- writeが必要になった場合だけ、対象repo内で`workspace-write`へ変更する。command networkは
  原則offのままとし、`danger-full-access`、`--dangerously-bypass-approvals-and-sandbox`、
  `--ask-for-approval never`、`dangerously_allow_all_unix_sockets`を使わない。
- Docker preflight、`apply`、`doctor`は、operator bootstrap済みの個人管理者が同じ非root identityで
  別terminalから実行する。sudoはpackage／group等のhost整備だけに限定する。trusted checkoutと
  管理者所有projectを使い、agentへはsanitized出力だけを返す。これにより
  on-host支援を検証しても、agentへrootful Docker controlを直接またはagent-written code経由で
  渡さない。
- 実験後は`codex logout`し、専用userのlocal credentialを除去する。別途作成したAPI keyや
  GitHub tokenは発行元でrevokeする。local logoutだけをrevokeとみなさない。
- 実行command、approval、human-run mutation、残したcredentialの有無をhandoffへ記録する。

Rootless Dockerはdaemonとcontainerを非root user namespaceで動かす公式の緩和策だが、現行の
McRemote apply / volume / backup契約との互換性は未検証である。オンホストagentを通すために
deployment profileを黙ってRootlessへ変えず、別sliceで評価する。

次のいずれかに当たる場合、オンホスト実験を開始または継続しない。

- 既存の個人管理者user、`sudo` user、rootful `docker` group memberでしか実行できない。
- sandbox enforcementやworkspaceを確認できず、full accessが必要になる。
- production world、backup、秘密情報を同じuserが読める。
- personal credentialの常設やSSH agent forwardingが必要になる。
- 人間の別terminalへprivileged stepを戻すと目的を満たせない。

対象host上agent用promptは、上のgateを人間が確認した後にだけ使う。

```text
これは対象host上agentの限定実験です。標準bootstrap経路ではありません。
現在のsandbox、workspace、OS userの管理group所属を秘密値なしで確認し、
read-onlyから始めてください。sudo、Docker socket、SSH agent、credential storeへ
accessせず、privileged commandは生成するだけにしてください。
私のtrusted checkoutやdeployment projectへ書き込まないでください。
applyとdoctorは私が管理者所有の別checkoutから別terminalで実行し、sanitized出力を返します。
gateを満たせなくなったら権限拡張を求めず停止し、human-runへhandoffしてください。
```

## 5. 人間とagentの分担

| phase | agentが担当できること | 人間が握るcheckpoint |
| --- | --- | --- |
| target | read-only接続確認、user / OS / toolの観測 | どのhost・userを対象にするか、agentに許すscope |
| SSH | 設定案、`sshd -t`、session別の結果整理 | 別terminalのSSH / `sudo -v`成功を自分で確認してからhardeningを承認 |
| toolchain | operator bootstrapのcheck、repo tests | `--install`、docker group付与、persistent host変更を承認 |
| project init | 全instance値を明示したcommand生成、validate | environment / volume / world / bind portを自分の言葉で確認 |
| agreements | EULA link提示、必要なfieldの場所を案内 | EULA同意、unverifiedを使う具体的理由を書く |
| resolve / plan | exact preset解決、artifact fetch、差分の要約 | lock identity、artifact、volume、world、port、warningをreview |
| render / apply | canonical render確認、承認後のexact apply、失敗時の診断 | review済みlock identityを渡し、host mutationを明示承認 |
| doctor | read-only runtime / hello確認、expected / observedの対比 | 何が動いたと判断でき、何が未確認かを説明 |
| pairing | pair開始後の状態観測、sanitized記録の補助 | Minecraft内command、pair codeの取扱い、実player操作 |

agentがcommandを実行したことと、利用者が判断できたことは別である。少なくともplanとdoctorでは、
agentは次を利用者へ問い返す。

```text
何が起きると予想したか。
実際にどの行がその予想を支えたか。
WARNは何を未確認だと言っているか。
次の操作で何が変わるか。
```

長文回答を要求する必要はない。画面の1行、短いメモ、スクリーンショット等から観察を残し、
そこから言語化を支援する。

## 6. explicit plan / apply boundary

read-only discovery、validate、plan、doctorと、host mutationを分ける。

- agentは`plan`で表示されたlock identityをambient stateから自動的にapplyへ渡さない。
- 人間がlock、volume、world、bind portをreviewした後、そのidentityを明示入力する。
- `--yes`や`--allow-unverified`を便利な既定値としてpromptやaliasへ埋めない。
- apply後の状態確認にapplyを再利用せず、`doctor`を使う。
- unknown container / volume、port conflict、stale renderをagentが自動修復しない。

管理端末 + SSH経路では、人間が承認した後にagentがexact commandを実行し、結果を検証してよい。
対象host上の限定実験では、Docker / apply / doctorとhost整備のsudoをこの規則の対象にせず、人間の
trusted operator terminalへ戻す。人間関与はagentへ作業を戻さないための儀式ではなく、判断とmutationの
境界を人間が握るために置く。

## 7. pause / handoff

作業を中断するときは、次を短く残す。

```markdown
- target: private inventory側の参照。公開repoへhost名/IPを書かない
- mode: human-run / 管理端末 + SSH / 対象host上の限定実験
- checkout commit:
- project / generated path:
- last PASS:
- expected warning:
- host mutation済み:
- 未確認:
- 次のhuman checkpoint:
- 次のexact command:
```

公開可能な未完了はrepo `NOTES_ja.md`、private inventoryは`mc-remote-backstage`、秘密を含むrawは
Git外へ分ける。セッションを跨いで必要だが着地前の素材はgitignored
`handoff-materials/<handoff_id>/`へ置き、`MANIFEST_ja.md`と`materials/`を持たせる。
これは正式evidenceではなく、着地確認後に削除する。

## 8. 完了主張の境界

agent-assisted bootstrapとdoctorがPASSしても、次を自動的に主張しない。

- compatibility verified
- pairing / authorization / LuckPermsの実機確認
- backup / restore可能
- upgrade / rollback可能
- public exposureやfirewallが正しい
- 利用者が一人で復旧できる

どの主体が実行したかより、何を観測し、どこまで反証可能に検証し、何を未確認と残したかを正とする。
