# private ops リポジトリ（運用台帳・設定控え）

McRemote 環境の非公開情報（ホスト接続先・DNS・稼働中 deployment の構成控えなど）を管理するための **非公開（private）リポジトリ** です。  
共通の構築手順・コマンドは [`mc-remote-stack`](https://github.com/Naohiro2g/mc-remote-stack) を参照します。

## 構成

- [inventory.md](inventory.md): 管理しているホスト、DNS、稼働中／片付け待ち deployment の一覧
- [deployments/](deployments/README.md): 各ホストで稼働している構成ファイル（`mc-remote.toml` / `mc-remote.lock.toml` / `operator/`、単体 Compose 構成は `compose.yaml` 等）の控え
- [AGENTS.md](AGENTS.md): 担当エージェント向けの作業境界
- `.gitignore`: 秘密実値・生ログ・バックアップ実体を Git に入れないための除外設定

## 運用の要点

- **秘密実値（トークン・パスワード・秘密鍵・未マスク生ログ）は Git に置かず**、ホスト側の secret store や Git 外の安全な保管先で扱います。
- 運用に合わせて契約メモ、移行計画、作業ノートなどを自由に追加して構いません。
