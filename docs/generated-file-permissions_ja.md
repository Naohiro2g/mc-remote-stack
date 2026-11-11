# 生成fileの権限

`mcrctl`のCompose生成物は、実行元のumaskに依存せず、containerに渡す入力だけを読取り可能にします。
根拠はknowledgeの[権限修正の依頼](https://github.com/Naohiro2g/mc-remote-knowledge/blob/f29ce18129bd6b64eb2dbcaef7b4d89e44c99f7a/00-hub/mcrctl-file-mode-instructions_ja.md)です。

## containerへ渡す生成物

| pathの型 | 作る処理 / 読む側 | 秘密の実値 | file / directoryのmode |
| --- | --- | --- | --- |
| `generated/Caddyfile` | TOML render / Caddy | なし | `0644` |
| `generated/runtime/scratch.json` | TOML render / Scratch HTTP server | なし。公開browser設定 | file `0644`、`runtime/`は`0755` |
| `generated/runtime/web-pairs/*.json` | TOML render / Caddy | なし。公開browser設定 | file `0644`、配下directoryは`0755` |
| `generated/minecraft/server.properties` | TOML render / Minecraft初期化処理 | なし。生成seed | file `0644`、配下directoryは`0755` |
| `generated/minecraft/plugins/McRemote/config.yml` | TOML render / Minecraft初期化処理 | なし。認証backendのpath等の既定値のみ | file `0644`、配下directoryは`0755` |
| `generated/wirescope/**` | 検証済み公開bundleからTOML render / Caddy | なし。公開Web assets | file `0644`、配下directoryは`0755` |
| compact interfaceの`Caddyfile`と`runtime/scratch.json` | compact render/preview / Caddy・Scratch | なし | file `0644`、配下directoryは`0755` |
| compact interfaceの`runtime/minecraft/plugins/McRemote/config.yml` | compact render/preview / Minecraft初期化処理 | なし。生成seed | file `0644`、配下directoryは`0755` |

これらのdirectoryは個別にbind mountされるため、containerはhost上のprojectやrender rootを走査する必要はありません。
render root自体は`0700`に保ちます。生成物がoperatorと異なるUIDのcontainerから読まれる場合も、公開入力のread/traverse bitで対応します。
Caddyはroot UIDでも、capabilityを落としている場合にはfile所有者のアクセス制御を迂回できません。

## hostだけが扱うfileと、既存の保護を維持するもの

| pathの型 | 作る処理 / 読む側 | 秘密の実値 | modeの扱い |
| --- | --- | --- | --- |
| render内の`compose.yaml`、`render-manifest.json`、compactの`mc-remote.lock.json` | render / operator・Docker Compose CLI | 現在の生成内容にcredentialはないが、公開入力として扱わない | `0600` |
| compactの`current.json` | interface apply / operator・doctor | 状態metadata | 従来どおり`0600` |
| 未分類の新しいrender file | render / 未分類 | なしとは仮定しない | `0600`、専用directoryは`0700`。公開入力を増やすときは明示分類する |
| projectのorder/lock/operator入力 | init/resolve/update / operator | typed input・参照情報。privateに扱う | 既存fileのmodeを維持。新規project fileは従来の`0640 & ~umask`、project rootも従来の保護を維持 |
| updateのsource/candidateコピー・追加Compose snapshot | update plan / update・operator | overlayに秘密が含まれる場合がある | コピー元fileのmodeを維持。追加snapshotのdirectoryは`0700` |
| updateのstate/active/transaction lock | update / operator | 参照・進行metadata | 従来の`0640 & ~umask`と、非公開の準備directoryを維持 |
| CASのJAR/公開artifact | fetch/import / Minecraft初期化・Java等 | runtime配布物 | 従来どおりfile `0644`。host側のCAS親directoryはcontainerへmountしない |
| homepageの検証済み公開tree | homepage import/sync / Caddy | 公開用に選んだ内容 | 従来どおりfile `0644`、公開tree内directory `0755` |
| secret store | secret set/read / operator | あり | 従来どおりfile `0600`、保存directory `0700` |
| backup archiveのdownload/decrypt・暗号文/transfer記録 | backup / operator | world・credential等を含み得る | 従来の`0600`保護を維持。公開権限の正規化対象外 |
| 復元の一時world/rollback directory | world restore / 復元処理 | world/player情報を含み得る | 従来の`0700`保護を維持。公開権限の正規化対象外 |

稼働volume内のMcRemote設定・snapshot・authority、LuckPerms DB、Discord/Geyser設定等を、生成treeの権限処理で変更しません。
内容を検査して「秘密がなさそう」と推測する方式ではなく、生成するpathの役割で分類します。

## 更新と復帰

renderはfileのmodeを明示して書き、公開入力の親directoryを生成tree内に限って正規化してからpublishします。
更新と自動復帰のどちらも同じrender処理を使います。呼出し元のumaskを変更する処理や、project全体への再帰chmodは使いません。
追加Compose snapshotと新しいoperator入力のコピーは、一時fileのmodeをコピー元に合わせてから書き、atomic replaceします。
`copyfile`後にchmodする方式の、一時的に読取範囲が広がる状態も避けます。

同じ内容でも生成treeのmodeが正しくない場合、renderはtreeを置き換えて修復します。
このmodeだけの修復ではfileのmtimeを保持し、Minecraftのseed同期で「新しい設定」と判定されないようにします。
内容とmodeが両方同じなら従来どおり`unchanged`です。lock照合はbytesを基準とするため、旧版で生成したsourceからも更新できます。
この修正をmergeしただけでは公開環境のfileは変わりません。次の明示的なrender/更新から新しいmodeを使います。
