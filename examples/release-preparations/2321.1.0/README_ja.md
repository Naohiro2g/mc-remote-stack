# 2321.1.0 公開前のStack準備

`public-web-paper@18.toml`は、公開Releaseを待つpresetの草案です。
McRemoteの`origin`が未記入なので、そのまま解決・配備するpresetではありません。
公開catalogやregistryへはまだ登録していません。

基準はStack main `2b5c04842e78b9767c31903e9d8328d7554ce702`、knowledgeの作業契約は
`088b7343ae71ef2c2259bf73a3e2f2e9e1b13546`の`2321.1-set-1`とStackへの作業指示です。

- profileは`vps-server@18`を継続します。runtime、heap、world、Bridge、WireScopeは継続します。
- Paperは26.3 #244、McRemoteは2321.1.0、Scratchは指定された候補OCIへ更新する草案です。
- `plugins.toml`は、McRemoteを除く9本のtyped inputです。DirectionHUD／OtterLibを外し、Geyserを2.12.1-b1294、LuckPermsを5.5.91へ更新します。ほか7本は現行と同じ版です。
- Geyserは公式APIのSHA-256と取得JARを照合しました。LuckPermsは公式downloadのbuild 1678のJARからSHA-256を計測しました。

公開後、次の確認を終えてから正式presetを追加します。

1. [Release収集手順](../../../docs/release-preset-preparation-guide_ja.md)に従い、McRemoteの公開manifest・assetを取り直し、凍結JARのbytes／SHA-256と照合して`origin`を埋めます。CI artifactのURLは正式presetへ入れません。
2. Scratchのブロック一覧の追加後にcoordinatorが取り直す凍結setと公開manifestを確認し、草案の`source_commit`、OCI digest、runtime contractを照合します。contractのschema／fixtureが同じでも、公開manifestのsource commitに対応する収容が必要です。
3. 完成したpresetを`src/mc_remote_stack/data/preset_registry/public-web-paper/18/preset.toml`へ追加し、Scratch contractを収容してcatalogを再生成します。`@17`は変更しません。
4. pytest、ruff、catalog／append-only検査を通します。mergeと公開箱庭への適用は別の作業指示に従います。

pluginの取得元：

- [Geyser 2.12.1-b1294の公式metadata](https://download.geysermc.org/v2/projects/geyser/versions/2.12.1/builds/1294)
- [LuckPerms 5.5.91の公式download](https://download.luckperms.net/1678/bukkit/loader/LuckPerms-Bukkit-5.5.91.jar)

このdirectoryには公開VPSの接続先、config、world、credential、Discordのtokenを収容しません。
