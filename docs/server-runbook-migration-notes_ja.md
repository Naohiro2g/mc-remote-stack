# 旧 server-runbook の振り分け notes

旧 private `server-runbook` は 2026-07-03 時点の実ホスト観測と、native-systemd 前提の移行案を
同じリポに持っていた。2026-07-21 の役割分離後は、このリポが公開 package / runbook、
`mc-remote-backstage` が private ops、旧リポが frozen history を担当する。

## carry map

| 旧内容 | 着地 | 処置 |
| --- | --- | --- |
| `ja/runbook_first_boot.md` / `ja/runbook_base_server.md` の個人管理者・SSH 安全柵 | 本リポ | [fresh-host bootstrap](fresh-host-bootstrap-guide_ja.md) へ一般化 |
| `ja/runbook_oss_install.md` の「public 手順と private 実値を分ける」原則 | 本リポ | 本リポと backstage の役割境界として反映 |
| provider、RAM、実 IP、UFW、listen port、導入済み tool の観測 | private backstage | 2026-07-03 の未再検証 snapshot として収容 |
| 2026-07-14 official VPS migration / recovery / public end-to-end | 本リポ + public knowledge evidence | 6GB VPSのCompose、Caddy、Scratch、Bridge、Minecraft、backup、pairing、rollbackの実証範囲を現行contractとrunbookへ再著作 |
| 2026-07-20 official VPS beta rollback rehearsal | 本リポ + public knowledge evidence | exact-lock deploy → rollback → smoke → same-hash redeployのtransactionをPhase 2設計へcarry |
| 6GB official VPSの実inventory、SSH alias、現行deployment path / hash | private backstage | 2026-07-03無料2GB snapshotと分離し、current opsとして再確認して更新 |
| 契約・実 host・private GitHub access の今後の運営 | private backstage | current ops として再検証してから更新 |
| native systemd / package Caddy / `/opt/.../current` symlink deploy | frozen archive | Compose・生成設定中心の現行実装と競合するため不採用 |
| GitHub Actions の候補 flow | frozen archive | 手動 deploy 前提の未検証案で、現行 apply contract が未実装 |
| Codex / Claude Code install・認証・private repo access | frozen archive | server package の要件ではなく、vendor 挙動と version に依存 |
| McRemote repo review、handoff、英語後追い翻訳 | frozen archive | 2026-07-03 の作業文脈・旧 topology・重複本文 |

## 現行 runbook にしなかった理由

旧文書の「DECIDED」は旧リポ内の当時判断であり、現在の `mc-remote-stack` 実装より優先しない。
特に production を native systemd first とする判断、Caddy を host package として直接管理する判断、
service ごとの release symlink は、現行 Compose / rendered configuration / immutable artifact gate と同時に
正本化できない。

却下した処置:

- 旧 `ja/` をそのまま `docs/` へコピーする: private 実値と superseded architecture を公開 current 手順にしてしまう。
- 実 IP だけ伏せて公開する: 可視性だけ直しても、実装との不一致は直らない。
- 旧リポを live SSOT のまま残す: stack と二重正本になる。
- private archive へのリンクだけを公開手順にする: public 利用者が手順を完結できない。

旧リポは provenance と却下理由を保つ frozen history であり、本リポの実行時依存先ではない。

## 2026-07-26 carry漏れの再確認

最初の公開化では、backstageへ2026-07-03の無料2GB VPS snapshotだけをcarryし、実際に構築・移行・
rollback検証した6GB official VPSのcurrent inventoryとsanitized evidenceへの入口を落としていた。
このため「archiveをcurrent contractにしない」という正しい境界が、「archiveにしか残っていない
有効な観測も再著作しない」という誤った処置になった。

再確認で次を照合した。

- 6GB official VPSはUbuntu 24.04、Docker Compose構成で実在し、Caddy / homepage /
  Scratch stable・beta / Bridge / Minecraftを稼働した
- 2026-07-14にpublic HTTPS / WSS / Minecraft / McRemote、recovery archive、human pairing、
  graceful rollbackがPASSした
- 2026-07-20にexact-lock betaのdeploy → rollback → smoke → same-hash redeployがPASSした
- 現在の実hostにも当時のactive beta Composeと同じSHA-256のgenerated構成が残っている

carry方法はarchive全文の復活ではない。次へ分ける。

| 情報 | 現行の着地 |
| --- | --- |
| provider、契約、IP、SSH alias、実path、稼働中hash | backstage current inventory |
| publicに説明できるtopology、gate、rollback原則 | 本リポの[public VPS bootstrap](public-vps-bootstrap-guide_ja.md) |
| human / live-autoのsanitized検証記録 | public knowledge `14-evidence`へowner handoff |
| exact service / artifact / volume契約 | profile / preset / lock / renderer / test |
| 秘密を含むraw、credential、private key | Git外 |

今後のarchive棚卸しは、file単位で「carry / reject」を決めるだけでなく、観測・決定・実装契約・
private実値・rawの粒度へ分解する。rejectした全文の中にcarryすべき有効な観測が残っていないかを
確認し、現行着地先と照合できるまで移行完了としない。

## 開発リポへの影響

- `McRemote` / `minecraft-remote-api` / `scratch-editor` / 本リポの agent bootstrap は、引き続き
  public `mc-remote-knowledge` を入口とする。今回の振り分けだけを理由に再配布しない。
- dev リポの README、Issue、CI、test fixture から archive / backstage の参照や実 host 値を要求しない。
- private ops の判断が protocol、config schema、port の公開範囲、release gate に影響する場合は、
  公開可能な interface と why を knowledge へ carry してから開発へ渡す。
- instance 固有の desired state / lock / secret 参照は deployment project に置き、本リポの既定値や
  dev リポの test fixture に昇格させない。
- 旧 systemd / package Caddy / release-symlink 手順を再利用するときは current contract とみなさず、
  現行の Compose / generated configuration と衝突しない新しい設計としてレビューする。
