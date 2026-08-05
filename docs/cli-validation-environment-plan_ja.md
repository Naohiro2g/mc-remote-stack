結論として、ほとんどのCLI開発はVPSで行う必要はありません。VPSは「公開環境固有の最終検証」に限定し、現在のベータは動かしたままにするのがよいです。

環境ごとの役割は次の分担が適しています。

| 環境 | 主な役割 |
|---|---|
| ローカル開発環境 | schema、lock、render、失敗系、FTPS fake、CLI出力のunit/deterministic検証 |
| ケータリングPC | 破壊可能なbootstrap、upgrade、rollback、restore、CLI install、network構築 |
| ホームサーバー | 長期稼働、plugin設定、backup timer、複数project、実world cloneによる統合検証 |
| VPS | public bind、Caddy/TLS/WSS、provider firewall、実FTPS、最終migration evidence |

## VPSが必要なもの

VPSでなければ十分に検証できないのは限定的です。

- public HTTPS／WSS／Caddy経路
- provider firewallと`DOCKER-USER`を含む公開到達性
- 現在使用している実FTPS accountとの最終往復
- 現行public betaをcanonical構成へ移す最終upgrade
- public betaとしてのsanitized live evidence
- DNS・証明書・外部Scratchからの接続

これら以外は、VPSで先に試す合理性はあまりありません。

## ホームサーバーが適するもの

ホームサーバーは、ケータリングPCより長期間動かす必要がある検証に向いています。

- plugin compositionの実運用確認
- DiscordSRV、Geyser、Floodgate、LuckPerms、ServerBackup設定
- plugin data directoryと所有権
- backup timerの数日間運転
- local retention
- upgrade後の継続稼働
- doctorの定期実行
- 複数deploymentの衝突検出
- world restore後の実プレイ確認

ただし、既存のhome-beta／alphaを直接実験対象にはしません。別のlab projectを用意し、次を完全分離します。

- project directory
- Compose project名
- volume
- world identity
- Java／McRemote port
- backup outbox
- secret namespace
- systemd unit名

既存world volumeをread-write mountせず、必要ならbackupから複製したworldを使います。

## ケータリングPCが最も適するもの

破壊や再構築を伴うCLI検証の主環境には、ケータリングPCが最適です。

特に次をまとめて検証できます。

1. CLI install／PATH契約
2. clean bootstrap
3. plugin composition
4. McRemoteオリジナルserver template
5. upgrade／reapply
6. 起動失敗時rollback
7. world restore
8. restore後のmanual rollback／finalize
9. backup service/timerのinstall
10. host-level multi-project collision
11. Ethernet APを含むnetwork topology
12. Internet不通・再接続時のartifact／plugin挙動

再構築可能なので、途中でvolumeやcontainerを意図的に壊すfailure injectionもできます。現在のVPSやホームサーバーでは避けたい試験です。

ケータリングPCで使うbackup／FTPSは、本番credentialではなく専用のtest endpoint・test identityに分けるのが適切です。

## 現在のVPSを動かしたまま並行できること

かなりあります。

安全に並行可能なのは次です。

- 別projectでの`init / resolve / fetch / render / plan`
- 現行runtimeのread-only doctor
- `runtime audit-log`
- `backup list`
- remote record／ciphertextのdownload
- 別pathへのdecrypt・archive inspect
- retentionのdry-run／plan
- external doctorの開発
- 新plugin lockの準備
- migration planの生成

短時間の別Minecraft instanceも、完全に別port・volume・worldであれば技術的には可能です。ただし事前にmemoryとportを確認し、現在のbetaと同時起動する価値がある場合だけにします。

一方、現在のVPSで二つ目の完全な`vps-server@5`をそのまま起動するのは適しません。Caddyが80/443を使用するため衝突します。追加Compose overrideで無理に回避すると、再びcanonicalでない検証になります。

同一VPSで正式なpublic stagingを並行稼働させるなら、先に以下を設計する必要があります。

- shared edge Caddy
- environment別hostname／route
- environment別Minecraft port
- 別volume／backup／systemd unit
- multi-project collision check

これは将来のCLI/profile機能として扱うべきで、今回のplugin canonical化の前提にはしない方がよいです。

## 推奨する進め方

1. ローカルでplugin compositionとauth-enforcement transactionを実装
2. ケータリングPCで破壊的bootstrap／migration failureからのrepair / resume／restoreを検証
3. ホームサーバーの別lab projectで長期運転
4. VPSでは現行betaを維持したままmigration planを準備
5. 十分に固まった段階で、人間checkpoint付きで現行VPSをcanonical構成へ移行
6. 最後にpublic HTTPS／WSS／FTPS evidenceを取得

したがって、今すぐVPSの現行ベータを触る必要はありません。次の主戦場はローカル実装とケータリングPC、ホームサーバーは長期統合、VPSは最後の公開環境検証という分担が最も安全で効率的です。
