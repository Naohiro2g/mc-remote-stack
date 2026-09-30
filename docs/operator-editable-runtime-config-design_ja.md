# 運営者が調整する設定ファイルの扱い

Stackが生成するファイルは2種類に分かれ、扱いが違います。

| 種別 | 例 | 扱い |
| --- | --- | --- |
| 配布物 | `paper.jar`、プラグインの`.jar`、OCI image | versionとSHA-256／digestで固定する。運営者は変更しない |
| 運営者が調整する設定 | `server.properties`、`plugins/McRemote/config.yml` | Stackが良い初期値で最初に一度だけ置く（seed-once）。その後の運営者の編集は上書きしない |

Dockerを使う価値は、初期設定が環境に左右されず、簡単で確実なことです。運営者の調整を再起動のたびに消してしまうと、その価値を損ないます。

## 仕組み

Minecraftのcontainer（itzg/docker-minecraft-server）は、起動時に`/config`の内容を`/data`へ同期します。Stackは次の2つを設定しています。

- `SYNC_SKIP_NEWER_IN_DESTINATION=true`: `/data`側のファイルの方が新しければ上書きしない。運営者が稼働中に編集した設定は、再起動しても残る。
- `REPLACE_ENV_DURING_SYNC=false`: 同期の際に環境変数で書き換えない。

この設定は、公開VPS用の`_compose_public`（`compose@13`）と、非公開ネットワーク用の`_compose_v14`の両方にあります。

## Stackの新しい初期値を反映したいとき

新しいpresetへ更新すると、Stackは新しい設定を`/config`に置きます。ただし、`/data`側で運営者が編集したファイルが新しい場合は、そちらが残ります。新しい初期値を使いたいときは、`/data`側の該当ファイルを削除してから再起動します。

## 動作の確認

`mcrctl doctor`は、生成したファイルそのものではなく、稼働中の結果を確認します。たとえばMcRemoteの認証は、トークンなしの`protocol.hello`が認証を要求されることで確かめています。
