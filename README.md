# mc-remote-stack

> [!NOTE]
> このリポジトリの文書は日本語を正本とします。方針は
> [LANGUAGE_POLICY.md](https://github.com/Naohiro2g/mc-remote-knowledge/blob/main/LANGUAGE_POLICY.md)
> を参照してください。

McRemote（マイクラリモコン）のサーバー一式を、同じ手順で何度でも組み立てるためのパッケージです。
設定ファイルから、versionとdigestを固定したDocker Compose構成を生成し、適用して、動作を確認します。
コマンドは`mcrctl`です。

## 組み立てるもの

| component | 役割 |
| --- | --- |
| Caddy | HTTPS／WSSの入口。ホームページも配信する |
| Scratch | マイクラリモコンのScratchクライアント |
| Bridge | ScratchからのWebSocketを、McRemoteプラグインのTCP接続へ中継する |
| Minecraft | Paperサーバー＋McRemoteプラグイン |

## 今の手順（公開VPS）

稼働中の公開VPS（beta）は、次の順で扱います。

1. [新しいUbuntu hostを準備する](docs/fresh-host-bootstrap-guide_ja.md)（hostを新しく用意したときだけ）
2. [releaseからpresetを作る](docs/release-preset-preparation-guide_ja.md)
3. [VPSを新しいpresetへ更新する](docs/public-vps-bootstrap-guide_ja.md)

日々の運用は次のページにあります。

- [ホームページを更新する](docs/homepage-sync-guide_ja.md)
- [backupを転送する・復元する](docs/backup-and-restore-guide_ja.md)

## 目指す形と、今との差

目指しているのは「ケータリング方式」です。「b8セットをVPSへ」のような短い依頼から、Stack担当が
設定ファイル`mc-remote.toml`を一つ用意し、`mcrctl apply`と`mcrctl doctor`の二つのコマンドで、
新規構築も更新も終えます。設計の正本はknowledgeの
[deployment interface設計](https://github.com/Naohiro2g/mc-remote-knowledge/blob/main/00-hub/deployment-interface-design_ja.md)です。

今との差は次のとおりです。

- この形のコード（`mcrctl apply ./mc-remote.toml`）はありますが、扱えるのはScratch、Bridge、
  Minecraftの三つだけです。Caddy、ホームページ、backup、追加プラグインはまだ扱えず、実機で動かした
  こともありません。実装の範囲は[deployment interface実装境界](docs/deployment-interface-implementation_ja.md)
  にあります。
- そのため公開VPSは、上の「今の手順」で動かしています。こちらはprofileとpresetを指定したproject
  （`mc-remote.toml`＋`mc-remote.lock.toml`＋`operator/`以下の入力）を、`mcrctl deployment update`で
  更新する方式です。
- 二つの方式は、設定ファイル名がどちらも`mc-remote.toml`ですが、書き方が違います。
- 次の環境の仕組みは、まだStackにありません: 公式stable（Minecraft部分はXServer GAMEs）、dev
  サーバーのalpha／beta、Dockerを使わない構成、ケータリングPCでの検証、private opsリポの立ち上げ。

## 役割分担

| リポジトリ | 持つもの |
| --- | --- |
| mc-remote-stack（ここ） | マシンの構築と運営の方式、公開できる手順。方式を確立していく間は、運用の実務も持つ |
| mc-remote-backstage（非公開） | 公式運用のprivate opsリポ。実際のhost、接続先、契約などの非公開情報。方式が確立した環境では、日々の実務を担う |
| [mc-remote-knowledge](https://github.com/Naohiro2g/mc-remote-knowledge) | 設計判断の正本 |

Stackとbackstageは協調して働き、どちらが実務を持つかを厳密には区切りません。

OSSとして使う場合は、非公開の運用情報を置くprivate opsリポを自分で持ちます。公開テンプレートは作らず、
その立ち上げの仕組みをStackに置く予定です。

## 開発

```sh
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run mcrctl --help
```
