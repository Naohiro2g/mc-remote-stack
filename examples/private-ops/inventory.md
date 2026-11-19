# 運用インベントリ

現在稼働しているホストと deployment の一覧です。  
各 deployment の正確なイメージ・プラグイン・入力値の控えは [`deployments/`](deployments/README.md) を正とし、この一覧へ二重転記しません。

## 1. ホスト一覧

| SSH alias | 用途・役割 | スペック目安 | Stack / Docker | メモ |
| --- | --- | --- | --- | --- |
| `vps1` | 公開 VPS | 6 GB RAM | `~/mc-remote-stack` / Docker | 外部公開（`*.example.org`） |
| `alpha1` | 家庭内検証サーバー | 16 GB RAM | Docker Compose | Tailscale Serve 等で検証 |

## 2. DNS ルーティング

| 公開ホスト名 | 種別 | 向き先（例） | 用途 | 切替予定・メモ |
| --- | --- | --- | --- | --- |
| `example.org` | A | `<vps1-ip>` | ホームページ | — |
| `scratch.example.org` | CNAME | `vps1.example.net` | Scratch | — |
| `wirescope.example.org` | CNAME | `vps1.example.net` | WireScope | — |
| `bridge.example.org` | CNAME | `vps1.example.net` | Bridge (WSS) | — |
| `sb.example.org` | CNAME | `vps1.example.net` | Minecraft 本体 | — |
| `unity-sb.example.org` | CNAME | `vps1.example.net` | Unity箱庭 | — |

## 3. Deployment 一覧

| プロジェクト名 | 配置ホスト | 実パス | 構成控え | 状態 | 扱い・片付け予定 |
| --- | --- | --- | --- | --- | --- |
| `public-main` | `vps1` | `~/mc-remote-deployments/public-main` | `deployments/public-main/` | 稼働中 | 常設 |
| `unity-alpha-trial` | `alpha1` | `~/mc-remote-deployments/unity-alpha-trial` | `deployments/unity-alpha-trial/` | 稼働中 | 検証継続 |
| `old-trial` | `vps1` | `~/mc-remote-deployments/old-trial` | — | 停止中 | 次回メンテ時に削除予定 |

## 履歴・メモ

- 過去の作業経緯は Git コミット履歴を参照してください（長く残したい調査メモがあれば別ファイルへ分けてこの節からリンクできます）。
