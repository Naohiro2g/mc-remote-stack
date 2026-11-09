# 2321.1.0 公開VPSのplugin入力

正式presetは[`public-web-paper@18`](../../../src/mc_remote_stack/data/preset_registry/public-web-paper/18/preset.toml)です。
McRemote／Scratchの公開Release `v2321.1.0`から配布物とmanifestを照合し、registry/catalogへ収容しました。
契約はknowledge `ccc4a6c6f614c354ce6b5f04a075e951ecf92e49`のStack箱庭作業票です。

`plugins.toml`はMcRemoteを除く9本のtyped inputです。DirectionHUD／OtterLibを外し、Geyserを2.12.1-b1294、LuckPermsを5.5.91へ更新します。ほか7本は現行と同じ版です。
Geyserは公式APIのSHA-256と取得JARを照合し、LuckPermsは公式download build 1678のJARを計測しています。

profileは`vps-server@18`を継続し、runtime、heap、world、Bridge、WireScopeは変えません。
公開箱庭では[通常の更新手順](../../../docs/public-vps-bootstrap-guide_ja.md)に従い、入力をproject外のreview用fileへ収容して`deployment update plan --replace-input`で渡します。
mergeとapplyの実施時期はhuman ownerが決めます。

- [Geyser b1294の公式metadata](https://download.geysermc.org/v2/projects/geyser/versions/2.12.1/builds/1294)
- [LuckPerms 5.5.91の公式download](https://download.luckperms.net/1678/bukkit/loader/LuckPerms-Bukkit-5.5.91.jar)

このdirectoryに公開VPSの接続先、config、world、credential、Discordのtokenを収容しません。
