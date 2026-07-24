# mc-remote-stack NOTES

確定前または別sliceへ送る作業だけを置く。private host名、IP、credential、account情報は書かない。

## 2026-07-24 home-beta bootstrap後

- [ ] operator CLI install / PATH契約を確定する。bootstrap期はcheckout内
  `.venv/bin/mcrctl`をexact pathで使う。既存Ubuntu環境とクリーン環境の両方で、
  `uv tool install`等の候補を比較してから公開runbookへ採る。
- [x] 実利用者のagent-assisted bootstrapをclean Ubuntu Server hostで検証した。
  human-runだけで完走可能なcommand経路を保ち、管理端末からSSHする支援経路では、
  SSH hardening、package導入、EULA、unverified理由、plan / lock review、applyを
  人間checkpointとして戻した。
- [ ] 対象host上agentは、重要dataを持たない再構築可能なhostと専用非特権userで限定実験する。
  `sudo` / rootful `docker` group / SSH agent forwarding / personal credentialを与えず、
  agent-owned scratchと管理者所有のtrusted checkout / projectを分離する。applyとdoctorを
  trusted側の別terminalへ戻した場合の実用性と学習価値を評価する。
- [ ] agent-assisted bootstrapの基準経路、管理端末からの支援経路、対象host上の実験境界、
  人間checkpoint、pause / handoff境界を
  knowledge `20-教材/ai-learning-design_ja.md`と
  `00-hub/llm-agent-boundary-guide_ja.md`へ搬送し、着地確認する。
- [ ] Rootless Dockerを安全性の候補として別sliceで評価する。現行apply、managed volume、
  backup / restoreとの互換性を確認するまで、agent利用のためにdeploymentを切り替えない。
- [ ] token無しhello以外のread-only / reversibleなprotocol command smoke範囲を決める。
  world変更を伴うtestは、復元境界と人間の学習価値を確認してから実施する。
- [ ] pairing、Minecraft内command、実player操作を`live-human`として実施し、正式根拠に使う回は
  knowledge `14-evidence`へrecord + sanitized artifactを搬送する。
- [ ] `mcremote-paper@1`のrequired claimsと今回のunit / live-auto結果を照合し、
  compatibility record追加を別変更として判断する。bootstrap成功だけで`verified`へ変えない。
- [ ] backup / restore、upgrade rollback、host-level multi-project collision、
  `lan-only` / firewall責任分界、別volume / worldの`home-alpha`を独立sliceで検証する。

公開可能な現時点の観測:

- 既存Ubuntu Desktop hostを再インストールせず、`home-server@2` /
  `mcremote-paper@1`のisolated `home-beta`を実機検証した。
- repo tests / Ruff、order / lock validate、repo check、canonical plan、
  healthy container、同一lockのno-op apply、protocol `21.0.0` helloがPASSした。
- `0c63076`を既存hostへfast-forwardし、対象host上でも202 tests / RuffがPASSした。
  exact checkout pathの`mcrctl doctor`でcurrent lock / canonical render、managed volume /
  healthy container、loopback限定port、protocol `21.0.0` / Minecraft `1.21.11` helloがPASSした。
  旧版projectのroot / tracked inputは人間が明示して`0750` / `0640`へ締めた。
- 管理端末上のagentがSSHするmodeで1台目を検証し、PATH前提、TCP LF quoting、
  private/public handoff分離の改善点を回収した。対象host上agentは正規modeとはせず未検証。
- clean Ubuntu Server 24.04 hostを、既存container / volume / imageなしの状態から
  管理端末上agent + SSH modeでbootstrapした。checkout `3589d35`で対象host上の
  205 tests / Ruff、exact artifact fetch、canonical renderのno-op再実行、review済みlockでの
  bootstrap apply、doctorのhealthy runtime / current render / loopback限定port /
  protocol `21.0.0` / Minecraft `1.21.11` helloがPASSした。
- clean bootstrapで、cloud-initの`50-cloud-init.conf`より後のSSH drop-inでは
  password loginを無効化できないことと、新規lockが`0644`になる不具合を発見した。
  SSH drop-inを先行評価させるrunbook修正と、新規lockを最大`0640`かつcaller umaskを
  緩めず作る実装修正を、それぞれ回帰テスト付きで反映した。
- 既存個人管理者userはrootful Dockerを操作できるため、安全側のオンホスト実験profileに
  合わない。1台目へagentをinstallせず、再構築可能な別hostで評価する。
- compatibilityは意図どおり`unverified`のまま。private inventoryとraw logは本repoへ置かない。
