# 公開サーバー周辺 plugin 構成テンプレート（Geyser・Floodgate・ViaVersion・ViaBackwards 等）

このディレクトリは、公開サーバーや公開マイクラ箱庭で「大体、何でもつながる」環境を支える周辺 plugin の導入手順・設定テンプレート・取得先ガイドをまとめた参考例です。

`mcrctl` は PaperMC 本体と McRemote プラグイン以外の周辺 plugin JAR を、すべて運用者が選ぶ `operator/minecraft-plugins/plugins.toml`（`minecraft-plugins@1`）として SHA-256 固定でマウントします。また `/data` 配下のプラグイン設定は運用者編集を尊重する seed-once 契約（`SYNC_SKIP_NEWER_IN_DESTINATION=true`）で扱うため、周辺 plugin の版数や設定値は `mcrctl` のコードへハードコードせず、このテンプレートと各 runbook から参照します。

---

## 1. 準必須 4 プラグイン（「大体、何でもつながる」構成）

Java 版・統合版（Bedrock）を問わず幅広いバージョンの Minecraft クライアントを受け入れるため、公開サーバーではまず次の 4 本を基本セットにします。

| プラグイン | 役割 |
| --- | --- |
| **Geyser**（Spigot 版） | 統合版（Bedrock）クライアントからの接続を受け付ける |
| **Floodgate**（Spigot 版） | 統合版プレイヤーの認証・Xbox 連携を扱う（Geyser と組で導入） |
| **ViaVersion** | 新しいバージョンの Java クライアントからの接続を通す |
| **ViaBackwards** | サーバー本体より古いバージョンの Java クライアントからの接続を通す |

`plugins.core-4.toml`（[plugins.core-4.toml](plugins.core-4.toml)）はこの 4 本だけを並べる場合の最小テンプレートです。実際の `filename` と `sha256` は、導入時点で各公式ダウンロードページを確認して取得した実物の値へ置き換えてください。

---

## 2. 取得先ページ一覧（ホームページ・ダウンロードページ）

配布元の変更や注意書きに対応できるよう、ファイル名や固定の直接 URL だけで判断せず、**取得前に必ずホームページとダウンロードページの注意書き（対応 Minecraft 版、対応 Java 版、JAR の種類）を読み、取得後・初回起動後のサーバーログで `ERROR` / `WARN` が増えていないか確認します**。

| 対象 | ホームページ | ダウンロードページ | 確認ポイント（注意書き・運用メモ） |
| --- | --- | --- | --- |
| **Paper** | <https://papermc.io/> | <https://papermc.io/downloads/paper> | 対応 Minecraft 版・Java 版と最新 build 番号を確認する |
| **Geyser・Floodgate** | <https://geysermc.org/> | <https://geysermc.org/download> | Paper 向け（Spigot 版）の最新 build を選ぶ。Geyser の設定例は後述 |
| **ViaVersion** | <https://viaversion.com/> | <https://hangar.papermc.io/ViaVersion/ViaVersion/versions?channel=Snapshot> | 追従を早めるため Hangar の **Snapshot（`-SNAPSHOT`）** チャンネルを使う。対応 Minecraft / Java 版と Hangar 上の版名（`+実行番号`）を記録する |
| **ViaBackwards** | <https://viaversion.com/> | <https://hangar.papermc.io/ViaVersion/ViaBackwards/versions?channel=Snapshot> | ViaVersion と揃えて Hangar の **Snapshot（`-SNAPSHOT`）** チャンネルから取得する |
| **LuckPerms** | <https://luckperms.net/> | <https://luckperms.net/download> | Modrinth 側は更新頻度が低いため **公式サイト（`luckperms.net`）の Bukkit 版** から取得する。後付け時は `mcr.online`/`mcr.offline` と `mcr.build.range`/`mcr.build.blocks` を合わせて設定する |
| **WorldEdit** | <https://enginehub.org/worldedit> | <https://modrinth.com/plugin/worldedit/versions?l=bukkit> | Paper / Bukkit 向けの対応バージョンを確認する |
| **AdvancedPortals** | <https://advancedportals.sekwah.com/> | <https://dev.bukkit.org/projects/advanced-portals/files> | BukkitDev の Files から **`-spigot.jar`** を選ぶ（`-legacy-spigot.jar` は Minecraft 1.13 未満用） |
| **DiscordSRV** | <https://discordsrv.com/> | <https://www.spigotmc.org/resources/discordsrv.18494/> | `discordsrv.com` は案内ページへ転送される。SpigotMC 側のダウンロード導線から最新 JAR を確認する |
| **ServerBackup** | <https://server-backup.net/> | <https://www.spigotmc.org/resources/79320/> | `/backup` 出力先と権限・スケジュール設定を確認する |

### 取得・SHA-256 固定・起動確認の流れ

1. 上表の各公式ページで注意書き（対応 Minecraft 版・Java 版・JAR 種別）を確認して JAR を取得し、`sha256sum` でハッシュを計測する。
2. プロジェクト外の作業用コピーで `plugins.toml` に `filename` と `sha256` を記録し、`mcrctl artifact import-reviewed` / `mcrctl deployment update plan --replace-input "minecraft-plugins=..."` 等へ渡す。
3. 初回起動後、Minecraft サーバーログ（`/data/logs/latest.log`）に新規の `ERROR` や対応漏れを示す `WARN` が出ていないか確認する。

---

## 3. 推奨設定（ViaVersion `block-versions`・Geyser NetherNet）

### 3-1. ViaVersion `block-versions`（1.20.5〜1.21.4 の遮断）

ViaVersion 起動時に次のような警告が出ている環境（Paper 26.3 等で古い中間プロトコルを受け付ける必要がない公開マイクラ箱庭）では、Minecraft のデータボリューム内の `/data/plugins/ViaVersion/config.yml` の `block-versions` を設定して再起動します（参考テンプレート: [ViaVersion/config.block-versions.yml](ViaVersion/config.block-versions.yml)）。

```yaml
block-versions: ["1.20.5-1.20.6", "1.21-1.21.1", "1.21.2-1.21.3", "1.21.4"]
```

- **なぜ `mcrctl` の自動生成へ埋め込まないのか**: `ViaVersion/config.yml` は設定項目が多く版ごとに自動マイグレーションされる上、Stack の `/data` は運営者編集を上書きしない seed-once 契約を採っているためです。
- 反映後はサーバー再起動ログから `The Vanilla protocol of Minecraft 1.20.5-1.21.4 is easily exploitable` 警告が消えたこと、および `session-only/` 配下のパーミッション（ディレクトリ `0700`・ファイル `0600`）が維持されていることを確認します。

### 3-2. Geyser `19132` / `nethernet`

`vps-server@16` 以降では Java 版（TCP `25565`）と衝突しないよう、Geyser は専用ポート `19132`（TCP/UDP 両方）で待ち受けます。データボリューム内の `/data/plugins/Geyser-Spigot/config.yml` を次のように設定します（[公開VPSの構築準備](../../docs/public-vps-preparation-guide_ja.md)も参照）。

```yaml
bedrock:
  port: 19132
  transport: nethernet
```

### 3-3. LuckPerms を後から追加する場合の注意

LuckPerms を入れていないサーバーへ後から導入する場合は、permission の `mcr.online` / `mcr.offline` だけでなく、meta の `mcr.build.range` / `mcr.build.blocks` も一緒に設定してください。meta が欠けると上限が `0` になり、ペアリングやチャットが通ってもブロック設置が拒否されます。
