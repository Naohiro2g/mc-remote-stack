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
