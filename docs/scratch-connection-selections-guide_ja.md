# Scratchの組ごとに接続先と既定を指定する

`vps-server@17`／`connection-targets@4`では、同じセットで配信するScratchごとに、接続先一覧と既定の接続先を指定できます。ScratchとWireScopeの組は`public-routes@3`に残し、接続先の選択を`operator/connection-targets/targets.toml`へ書きます。

この設定は、Scratchへ渡す一覧と既定を変えます。別hostのMinecraftへ接続するためのBridgeの経路や接続条件は、別の対応です。現行の生成物では、選ばれたsandbox名はすべて同じMinecraft containerの内部DNS別名になります。

## 1. 既存入力のコピーを編集する

対象deployment projectへ入り、既存入力をproject外へコピーします。稼働中projectのorder、lock、生成物は直接編集しません。

```sh
cd "<対象deployment project>"
TARGET_REVIEW="$(mktemp -d "${TMPDIR:-/tmp}/mc-remote-targets.XXXXXX")"
cp operator/connection-targets/targets.toml "$TARGET_REVIEW/targets.before.toml"
cp operator/connection-targets/targets.toml "$TARGET_REVIEW/targets.toml"
```

`$TARGET_REVIEW/targets.toml`を編集します。次は、両方のScratchに同じ二つの接続先を載せ、一覧の順序と既定を分ける例です。既存の`[[notices]]`は今回の運用者noticeを維持してください。

```toml
[[targets]]
id = "stable"
label = "安定版"
sandbox = "sb.example.org"

[[targets]]
id = "beta"
label = "公開ベータ"
sandbox = "sb-beta.example.org"

[[notices]]
heading = "お知らせ"
body = "この本文は今回の運用者noticeに置き換えてください。"

[[selections]]
scratch = "scratch.example.org"
target_ids = ["stable", "beta"]
default_target = "stable"

[[selections]]
scratch = "scratch-beta.example.org"
target_ids = ["beta", "stable"]
default_target = "beta"
```

`[[targets]]`は共通の接続先台帳です。各`[[selections]]`の`target_ids`は台帳のIDを参照し、書いた順序でそのScratchへ配信します。一方にだけ接続先を載せることもできます。`default_target`は、その組の`target_ids`に含まれるIDを指定します。

このprofileではScratchの既定はselectionから決まり、public routesの`minecraft`とは独立します。

`scratch`はURLではなくhost名です。public routesの主Scratchと`[[web_pairs]]`内の全Scratchに、それぞれ一つずつselectionが必要です。未配信の名前、重複、指定漏れは生成時に失敗します。web_pairsが空でも主Scratchのselectionが一つ必要です。接続先の存在しないID、空の一覧、一覧の外の既定も拒否します。

運用者noticeは全組に共通です。noticeだけを編集するときも、接続先台帳とselectionsを維持します。

## 2. 更新候補を生成して確認する

稼働中セットの更新では、reviewした入力を更新planへ渡します。対象releaseのexact presetは、今回のhandoffにあるものを指定します。この機能のために製品の配布物を変更する必要はありません。

```sh
mcrctl deployment update plan \
  --to-profile vps-server@17 \
  --to-preset "<今回のexact preset>" \
  --replace-input "connection-targets=$TARGET_REVIEW/targets.toml"
```

planは候補側のadapterを`connection-targets@4`へ更新し、元projectの入力は維持します。返されたplan IDの候補で確認します。

```sh
TARGET_PLAN_ID="sha256:<planが返したID>"
TARGET_CANDIDATE=".mcrctl/updates/${TARGET_PLAN_ID#sha256:}/candidate"
cat "$TARGET_CANDIDATE/generated/runtime/scratch.json"
cat "$TARGET_CANDIDATE/generated/runtime/web-pairs/scratch.example.org.json"
```

主Scratchは`runtime/scratch.json`、追加の組は`runtime/web-pairs/<scratch host名>.json`に出ます。上の追加ファイル名は、public routesで追加した名前へ読み替えます。各JSONの`connection_targets`の内容・順序、`default_sandbox`、組に対応する`wirescope_url`を確認します。

まだ稼働していない別セットでは、orderに`vps-server@17`と`connection-targets@4`を指定し、reviewしたファイルをそのセットのoperator入力へ収容します。[公開VPSの構築準備](public-vps-preparation-guide_ja.md#4-設定と配布物を確認する)のresolve・validate・renderで同じ生成物を確認します。

## 3. 適用後に全組を確認する

[VPS更新手順](public-vps-bootstrap-guide_ja.md#5-planを適用する)または[別セットへの切替](public-vps-cutover-guide_ja.md)に従って適用します。doctorは配信された各組のJSONをそれぞれの生成物と照合し、一覧や既定の取り違えも検出します。各Scratchを再読み込みし、一覧とWireScopeへのリンクも確認します。

Bridgeの許可先は、全組で選択した接続先の和集合です。台帳にだけ残してどの組でも選んでいない接続先は含めません。Bridgeの既定は主Scratchの既定です。組別の一覧は、接続先に対する組別のアクセス制御にはなりません。

同じScratchのURLで接続先を切り替える場合は、そのオリジンのブラウザー保存を使います。異なるScratchのオリジン同士では、ブラウザー保存は共有されません。
