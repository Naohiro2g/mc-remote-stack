# mc-remote-stack

[マイクラリモコン](https://mc-remote.com/)（Minecraft Remote / mc-remote）のサーバーを構築・運用するためのパッケージです。

🏠 **公式サイト**: [mc-remote.com](https://mc-remote.com/)

> [!NOTE]
> **🌐 言語方針について / Language Policy**  
> 本リポジトリは、一次情報（SSOT）の鮮度と正確性を保つため、日本語を正本として記述しています。多言語参加やIssue/PRの利用方針については [主要言語についての方針転換 / Language Policy](https://github.com/Naohiro2g/mc-remote-knowledge/blob/main/LANGUAGE_POLICY.md) をご覧ください。  
> *This repository is maintained in Japanese as its primary Single Source of Truth (SSOT). Multi-language contributions are welcome. Please see our [Language Policy](https://github.com/Naohiro2g/mc-remote-knowledge/blob/main/LANGUAGE_POLICY.md).*

## マイクラリモコンとは

マイクラリモコンは、コーディングでマイクラの世界を動かしながら「学び方を学ぶ」ためのオープンソースのツール群です。Scratch や Python などで書いたプログラムから、マインクラフトのサーバーへブロックを置いたり、プレイヤーを動かしたりできます。マイクラのアプリは Java 版でも統合版でも接続できます。

- サーバーのプラグイン（McRemote）
- 各言語のクライアント（Scratch、Python、Java など）
- 通信の中身を観察する WireScope
- サーバーの構築・運用パッケージ（mc-remote-stack、このリポジトリ）

はじめかた、考え方、ロードマップは[公式サイト](https://mc-remote.com/)へ。

## このリポジトリの役割

マイクラリモコン全体のうち、サーバーの構築・運用の仕組みを受け持ちます。マシンの構築と運営の方式（ケータリング方式など）の正本はこのリポジトリにあります。設定ファイルから、versionとdigestを固定したDocker Compose構成を生成し、適用して、動作を確認します。コマンドは`mcrctl`です。

## 組み立てるもの

| component | 役割 |
| --- | --- |
| Caddy | HTTPS／WSSの入口。ホームページも配信する |
| Scratch | マイクラリモコンのScratchクライアント |
| Bridge | ScratchからのWebSocketを、McRemoteプラグインのTCP接続へ中継する |
| Minecraft | Paperサーバー＋McRemoteプラグイン |

## 今の手順（公開VPS）

稼働中の公開VPS（beta）は、次の順で扱います。

release待ちの間に、別のセットを用意する場合は[公開VPSの構築準備](docs/public-vps-preparation-guide_ja.md)を進められます。稼働中セットを維持したまま、設定とファイル配置を実機で確認し、正式releaseが揃ったら[別セットへの切替](docs/public-vps-cutover-guide_ja.md)へ進みます。

1. [新しいUbuntu hostを準備する](docs/fresh-host-bootstrap-guide_ja.md)（hostを新しく用意したときだけ）
2. [releaseからpresetを作る](docs/release-preset-preparation-guide_ja.md)
3. [VPSを新しいpresetへ更新する](docs/public-vps-bootstrap-guide_ja.md)

日々の運用は次のページにあります。

- [Scratchのお知らせを編集する](docs/scratch-notice-guide_ja.md)（配備前の編集と適用後の表示確認）
- [ホームページを更新する](docs/homepage-sync-guide_ja.md)
- [backupを転送する・復元する](docs/backup-and-restore-guide_ja.md)

ホームで b7.post2 の新セットを用意する場合は、[ホーム用ケータリング手順](docs/home-catering-guide_ja.md)へ進みます。起動前に設定を生成して確認する段階から説明しています。

## 目指す形と、今との差

目指しているのは「ケータリング方式」です。「b8セットをVPSへ」のような短い依頼から、Stack担当が設定ファイル`mc-remote.toml`を一つ用意し、`mcrctl apply`と`mcrctl doctor`の二つのコマンドで、新規構築も更新も終えます。設計の正本はknowledgeの[deployment interface設計](https://github.com/Naohiro2g/mc-remote-knowledge/blob/main/00-hub/deployment-interface-design_ja.md)です。

今との差は次のとおりです。

- この形のコード（`mcrctl apply ./mc-remote.toml`）は、Scratch、Bridge、Minecraftの3サービスと、ホーム用のCaddyを含む4サービスを扱います。ホーム用はhost側のHTTPS／WSS転送を使います。ホームページ、backup、追加プラグインの取込はこの入口にはまだありません。ホーム構成の実サービス起動・browser接続は未検証です。実装の範囲は[deployment interface実装境界](docs/deployment-interface-implementation_ja.md)にあります。
- そのため公開VPSは、上の「今の手順」で動かしています。こちらはprofileとpresetを指定したproject（`mc-remote.toml`＋`mc-remote.lock.toml`＋`operator/`以下の入力）を、`mcrctl deployment update`で更新する方式です。
- 二つの方式は、設定ファイル名がどちらも`mc-remote.toml`ですが、書き方が違います。
- 次の環境の仕組みは、まだStackにありません: 公式stable（Minecraft部分はXServer GAMEs）、devサーバーのalpha／beta、Dockerを使わない構成、ケータリングPCでの検証、private opsリポの立ち上げ。

## 役割分担

| リポジトリ | 持つもの |
| --- | --- |
| mc-remote-stack（ここ） | マシンの構築と運営の方式、公開できる手順。方式を確立していく間は、運用の実務も持つ |
| mc-remote-backstage（非公開） | 公式運用のprivate opsリポ。実際のhost、接続先、契約などの非公開情報。方式が確立した環境では、日々の実務を担う |
| [mc-remote-knowledge](https://github.com/Naohiro2g/mc-remote-knowledge) | 設計判断の正本 |

Stackとbackstageは協調して働き、どちらが実務を持つかを厳密には区切りません。

OSSとして使う場合は、非公開の運用情報を置くprivate opsリポを自分で持ちます。公開テンプレートは作らず、その立ち上げの仕組みをStackに置く予定です。

## 開発

```sh
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run mcrctl --help
```
