# mc-remote-stack NOTES

確定前または別sliceへ送る作業だけを置く。private host名、IP、credential、account情報は書かない。

## 2026-07-24 home-beta bootstrap後

- [ ] Paperのdefaultに依存しないMcRemoteオリジナルのserver templateを別sliceで設計する。
  汎用`minecraft-server@1`のtyped instance設定とは分離し、教材・公開体験向けにアレンジした
  「箱庭」のgameplay、world、performance、表示文、初期contentを再利用可能なpresetとして定義する。
- [ ] McRemote server iconの既存規定を現行public SSOTから回収する。今回確認した公開knowledgeの
  関連箇所では規定を特定できていないため、記憶だけで再定義せず、approved knowledge handoffで
  出典を回収してからformat / size、asset provenance、immutable identity、render / install契約を
  上記server templateの近接仕様として確定する。
- [ ] `vps-server@4` / `public-web-paper@1`をVPS実機でbootstrapし、Caddy / Scratch /
  Bridge / Minecraft、internal app network、public bind、managed volume、token無し
  protocol helloを`live-auto`検証する。host / provider firewallとDNSは人間checkpointとして
  分離し、外部HTTPS / WSS readinessとhomepage artifactは後続claimとして記録する。
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
  knowledge owner向けsanitized draftとしてhandoffし、`20-教材/ai-learning-design_ja.md`と
  `00-hub/llm-agent-boundary-guide_ja.md`への着地確認をownerへ依頼する。
- [ ] Rootless Dockerを安全性の候補として別sliceで評価する。現行apply、managed volume、
  backup / restoreとの互換性を確認するまで、agent利用のためにdeploymentを切り替えない。
- [ ] token無しhello以外のread-only / reversibleなprotocol command smoke範囲を決める。
  world変更を伴うtestは、復元境界と人間の学習価値を確認してから実施する。
- [ ] pairing、Minecraft内command、実player操作を`live-human`として実施し、正式根拠に使う回は
  knowledge ownerへrecord + sanitized artifact draftをhandoffする。
- [x] `mcremote-paper@1`のrequired claimsとunit / live-auto結果を照合し、
  exact `home-server@2` subjectへcompatibility recordを追加した。bootstrap成功だけでなく、
  isolated runtimeのprotocol helloとlan-onlyのrender-only結果を主張境界付きで固定した。
- [ ] compatibility record追加前のunverified lockで稼働中のruntimeを、world volumeを維持したまま
  verified lockへ移すdeployed-state transactionを設計する。record追加はcandidate lock identityを
  変えるため、upgrade apply未実装の現状では対象host checkoutを単純更新・再resolveしない。
- [x] home private alphaを`home-server@2` / `mcremote-paper@2` /
  `alpha` / `isolated` / `integration`の別project・別volume・別world・別portでlive検証する。
  `mcremote-paper@2`はb2 exact artifactを使うdeployment-path検証用で、live evidence着地前は
  unverifiedを維持する。
- [ ] home private alphaのsanitized `live-auto`素材をknowledge ownerへhandoffし、
  正式evidence着地後にexact subjectのcompatibility recordを別変更で追加する。
- [ ] backup / restore、upgrade rollback、host-level multi-project collision、
  `lan-only` / firewall責任分界を独立sliceで検証する。
- [ ] official VPS betaのbackup / restore contractを現行TOML経路へ移す。2026-07-26観測では、
  `/var/lib/mc-remote/backup-beta/outbox`に毎日03:33のwhole-server ZIPが
  2026-07-21から2026-07-26まで6世代あり、各約786.5--787.0 MB、ownerはruntime UID/GID
  `10001:10001`だった。最新世代
  `backup-2026-07-26~03-33-00-..zip`はSHA-256
  `ec5dd15316e76036b8618c13278e802507c9ced102c24ab548fdb9bf0a94ea1c`、
  CRC OK、1525 entry、244 region、11 active plugin JARだった。world 3 rootとserver iconを
  current managed volumeへ復元し、Minecraft 1.21.11、11 plugin、doctor、public endpoint、
  Scratch betaからの接続・`postToChat`・`setBlocks`までlive確認した。
  外部生成元`/etc/mc-remote/generated/minecraft-beta/plugins/ServerBackup/config.yml`は
  `BackupDestination: /backup/outbox`、`DeleteOldBackups: 0`、`BackupLimiter: 0`、
  plugin FTP upload / local deleteともfalseだった。6世代・約4.7 GBは上限ではなく稼働日数の
  結果だった。復元後のbetaは00:30 / 04:30 / 08:30 / 12:30 / 16:30 / 20:30、
  `BackupLimiter: 6`、`BackupDestination: /backup/outbox`、plugin FTP無効で起動し、
  runtime UID/GID `1000:1000`からoutboxへ書込みできることを確認した。local plaintextは
  6世代となったが、adapter所有の暗号文 / transfer recordのretentionは別途決める。
- [ ] archive
  `mc-remote-knowledge-archive@54652a9e25c7535b637645a4b88a1543cc998006:
  40-サービス運用/server-package-design_ja.md`のbackup契約を回収候補として照合する。
  snapshot生成と外部転送を分離し、betaは03:33、producer outputは`/backup/outbox`、
  whole-server archiveはsecret-bearing、通常world restoreはcredential storeを上書きしない。
  archiveは現行SSOTへ未着地なので、そのまま実装根拠にせずknowledge ownerへ搬送する。
- [ ] off-host restore経路のlive smokeを完成させる。決定論的CLIにはage暗号化、
  explicit FTPS upload、remote size / download SHA-256検証、秘密を含まないtransfer record
  sidecarのatomic upload、明示remote list、record / ciphertext download、record照合、
  age復号と平文SHA-256照合を実装した。world-only restoreもunsafe / duplicate / symlink
  entry拒否、credential除外、current lock / render / managed volume束縛、staging、停止、
  cutover、start、doctor、失敗時rollback、成功時旧world保持まで実装した。実FTPS相手の
  sidecar付き往復は、最新betaと同じSHA-256のcopyをage暗号化し、explicit FTPSでatomic
  upload、remote再download SHA-256まで`download-verified`でPASSした。暗号文は
  787,157,433 bytes、SHA-256
  `744cedbc9febec5f42feeae75484e4af2b1286033851e847d03cbd0a21b776a4`。
  `backup drain`は明示activation markerより新しいarchiveだけを対象にし、120秒のstable age、
  ZIP CRC、検査中のidentity / size / mtime不変、`download-verified` recordによる重複防止を
  行う。systemd timerの初回はmarker以前の6世代を`none-ready`として除外し、手動で生成した
  新しいwhole-server ZIPだけを自動搬送した。暗号文は776,236,198 bytes、SHA-256
  `82eb7be298226b018bdac965c577e2fd398b2231245d57f02908487ce7c49100`、
  statusは`download-verified`。XServerはupload中の既知名`SIZE`を受理する一方、`NLST`後の
  `SIZE`を550にするため、remote listはsize factを持つ`MLSD`を優先し、非対応serverだけ
  `NLST` + `SIZE`へfallbackする。新CLIによるlive world restoreは未実施である。
  現行official public betaは一時的な`compose.recovery-plugins.yaml`を併用しているため、
  canonical composeだけで再起動するworld restore applyはplugin setを外す。restore preflightは
  追加Compose fileを検出して拒否する。live applyはplugin compositionを正規lockへ取り込んだ後に行う。
  TOML deployment用off-host transportはprivate mode-0600 fileを明示指定し、provider /
  account inventoryをpublic project orderへ混ぜず、passwordは既存secret storeから参照する。
  ServerBackup 2.10.0の公式仕様では`DeleteOldBackups`は日数、`BackupLimiter`が総世代数である。
  official betaは毎日00:30 / 04:30 / 08:30 / 12:30 / 16:30 / 20:30、local最新6世代、
  producer内蔵FTPは無効、完成archiveをage + explicit FTPS adapterで毎回転送する。
  VPS homeには2026-07-13 beta候補と2026-07-14 stable候補の`.zip.age` /
  `.transfer.json`が残り、両recordとも`download-verified`だった。一方、
  2026-07-21--26のMinecraft 1.21.11 beta最新6世代については、home配下に暗号文 /
  transfer record / 現行transport configを確認できず、off-host転送済みか未判定だった。
  XServer側のaccount rootはFTPS session内の`/`として観測され、旧形式のstable / beta
  backupは`mcrctl backup list`の対象外、新形式はlive smoke前に0件だった。
- [ ] plugin compositionを通常の非container運用に近い表現力へ上げる。JAR identityだけでなく、
  pluginごとのconfig template / override、secret注入、data directory所有権、load順・依存、
  runtime library / content download、update check、egress、backup / restore policyを
  lock / render / doctorへ投影する。今回の復元起動ではDirectionHUDがPaper libraryをMavenから
  取得し、GeyserがMinecraft JARを取得、ServerBackup / DiscordSRV / ViaVersionがupdate checkを
  行った。`archive inspect`はplugin descriptorのruntime library宣言をinventoryし、
  `runtime audit-log`は明示log eventをURL pathやraw lineなしで分類するが、network accessの
  不在証明やtransitive dependency lockではない。正式plugin setとplugin data復元範囲は
  判断待ちのため、現行recovery overrideを正規lockへ暗黙昇格しない。
- [x] WoL / WoWLANの重要性、非標準化の理由、検証境界を
  [`docs/wake-on-lan-field-note_ja.md`](docs/wake-on-lan-field-note_ja.md)へ公開した。
  WoLは準24時間運用で重視する一方、一般bootstrapの必須機能、profile capability、
  compatibility条件、b3開始gateにはせず、利用者の手元で再現不能なhardware条件を強制しない。
  異なるdesktop hardware 2台で、Python 3.12.3 / `wakeonlan 0.41`、directed broadcast、
  deep sleep / poweroffの相互8ケースとservice healthを確認済み。正式`live-human` evidenceは
  knowledge commit `4b8ab4b6e173053e4c9a167011d6ed0c8ae4bd1c`の
  `14-evidence/records/2026-07-25-ubuntu-desktop-wol-mutual-live-human_ja.md`へ着地した
  [→DEC 2026-07-25-08]。
- [ ] WoL / WoWLANのhardware-specificな外部技術記事化を続ける。
  同一hardwareのWindows Wi-Fi poweroff復帰はoperator観測あり、Ubuntu WoWLANは未成立として、
  firmware / NIC・Wi-Fi chipset / OS / kernel / driver / renderer / power stateを固定した事例にする。
  `Restore on AC Power Loss`やwatchdogをWoLの成功主張へ混ぜない。
- [ ] ケータリングキットのnetwork topologyは既存Ethernet接続hardware APを最初の検証routeとし、
  Wi-Fi-to-Ethernet bridge、内蔵WoWLAN、USB Wi-Fi / USB Ethernet、専用router PCを比較する。
  USB常時給電だけでremote wake可能とはみなさず、exact USB ID / chipset / driver、
  USB remote wake、broadcast透過、offline DHCP / DNS、client isolation、設定restoreを実機確認する。

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
- exact `home-server@2` + `mcremote-paper@1`はcompatibility record追加後の新規resolveで
  `verified`となり、unverified acknowledgementは不要になった。record追加前のlockで稼働するhostは
  migration transactionができるまで旧checkout / lockを維持する。private inventoryとraw logは
  本repoへ置かない。
- exact `home-server@2` + `mcremote-paper@2`のhome private alphaを、既存betaと別project /
  volume / world / loopback portでbootstrapした。対象checkoutで208 tests / Ruff、初回apply、
  同一lockのno-op apply、doctorのhealthy runtime / current render、protocol `21.0.0` /
  Minecraft `1.21.11` helloがPASSし、既存betaもhealthy / currentを維持した。正式evidence着地前の
  ためalpha presetは意図どおり`unverified`のままとする。
- 異なるdesktop hardware 2台を相互sender / targetとし、Python 3.12.3と
  `wakeonlan 0.41`からdirected broadcastへmagic packetを送った。両方向のdeep sleep /
  poweroff、boot ID、SSH、1または2 containersのhealthを8ケースすべて確認した。
  poweroffではSSH不能が完全消灯より先行するhostがあり、人間の完全消灯確認後に送信する
  二段checkpointが必要だった。MAC、IP、private host名は公開記録へ含めない。
