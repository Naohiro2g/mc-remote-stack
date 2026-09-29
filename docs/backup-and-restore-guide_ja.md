# backupの転送と復元

ServerBackupプラグインが作るサーバー全体のarchive（ZIP）を、暗号化してVPSの外へ転送し、必要なときに
取り戻してworldだけを復元する手順です。VPSの中にだけあるsnapshotは、off-host backupではありません。

コマンドは対象hostで、deployment projectのdirectoryに入って実行します。最初に環境を確かめます。

```sh
cd "$HOME/mc-remote-deployments/<deployment名>"
mcrctl operator check
```

`mcrctl`が見つからない場合は、[PATHが通っていないとき](fresh-host-bootstrap-guide_ja.md#pathが通っていないとき)で戻します。

## 1. 転送先を設定する

転送先はFTPS（明示的TLS）です。接続情報は、deployment projectの外に置くmode `0600`のファイルに書きます。
passwordはこのファイルにもprojectにも書かず、`mcrctl secret set`で保存します。

```toml
# /secure/path/backup-transport.toml（mode 0600）
schema_version = 1

[transport]
type = "ftps-explicit"
host = "ftp.example.jp"
port = 21
passive = true
tls_verify = true
username = "backup@example.jp"
credential = "secret://backup_ftps_password"
remote_directory = "/"

[transport.encryption]
type = "age"
recipient = "age1..."
```

```sh
mcrctl secret set backup_ftps_password
```

暗号化にはageを使います。転送するのはage暗号文だけで、復号に使うage identityはprojectとGitの外に保管します。

## 2. archiveを一つ転送する

```sh
mcrctl backup transfer /backup/outbox/backup.zip \
  --transport-config /secure/path/backup-transport.toml \
  --verify-download
```

archiveをageで暗号化してから、証明書を検証するFTPS sessionでuploadします。一時的な名前でuploadしてから
名前を変え、最終的なsizeを確かめます。`--verify-download`を付けると、remoteの暗号文を取り戻して
SHA-256も比べます。復元が転送元VPSに依存しないよう、秘密値を含まないtransfer record（`*.transfer.json`）を
暗号文と一緒に保存します。平文と手元の暗号文は削除しません。

## 3. 定期的に転送する

定期実行では、運用を始める前からあったarchiveを勝手に送らないよう、最初に一度だけactivation markerを
作ります。markerを作る前に、既存のarchiveを確認してください。

```sh
install -m 600 /dev/null /secure/state/backup-transfer-activated

mcrctl backup drain /backup/outbox \
  --after /secure/state/backup-transfer-activated \
  --transport-config /secure/path/backup-transport.toml
```

`drain`が送るのは、markerより新しく、更新から120秒以上たち、ZIPの検査中に変化しなかったarchiveだけです。
毎回remoteから取り戻してSHA-256を確かめ、確認済みのものは再送しません。平文、手元の暗号文、transfer record、
remoteの世代はどれも削除しません。手元に残す世代数は別に決めます。timerなどへの登録は運用者が行います。

## 4. 復元するarchiveを取り戻す

復元する世代は、必ず名前を指定して選びます。`latest`のような暗黙の選択はしません。

```sh
mcrctl backup list --transport-config /secure/path/backup-transport.toml

REMOTE_NAME='backup.zip.<encrypted-sha256>.age'
mcrctl backup download-record "$REMOTE_NAME" \
  --transport-config /secure/path/backup-transport.toml \
  --output ./recovery/backup.transfer.json
mcrctl backup download "$REMOTE_NAME" \
  --transport-config /secure/path/backup-transport.toml \
  --record ./recovery/backup.transfer.json \
  --output ./recovery/backup.zip.age
mcrctl backup decrypt ./recovery/backup.zip.age \
  --record ./recovery/backup.transfer.json \
  --identity /secure/path/age-identity.txt \
  --output ./recovery/backup.zip
```

`backup list`の`record=present`は、暗号文とtransfer recordが組でそろっていることを示します。
`record=missing`の世代は、remoteだけからは復元を始められません。これらのコマンドはremoteの世代を削除せず、
既存の出力ファイルを上書きせず、passwordやage identityを表示しません。

## 5. archiveの中身を確かめる

展開せずに、archiveのSHA-256、ZIPの検査結果、server JARと使用中プラグインのSHA-256を調べます。
プラグインの設定内容は表示しません。

```sh
mcrctl archive inspect ./recovery/backup.zip --json
```

## 6. worldだけを復元する

archiveから選んだworld（overworldと、あればNether／End）だけを、今のdeploymentへ戻します。
`--expected-archive-sha256`と`--expected-lock-identity`には、上で確かめた値と、今のlockの値を入れます。

```sh
mcrctl world restore plan ./recovery/backup.zip \
  --source-world world \
  --expected-archive-sha256 '<64-lowercase-hex>' \
  --expected-lock-identity 'sha256:<64-hex>'

mcrctl world restore apply ./recovery/backup.zip \
  --source-world world \
  --expected-archive-sha256 '<64-lowercase-hex>' \
  --expected-lock-identity 'sha256:<64-hex>' \
  --yes
```

applyはMinecraftだけを止めてworldを差し替え、起動してdoctorを実行します。起動またはdoctorに失敗したら、
元のworldへ戻します。成功しても、確認が終わるまで元のworldを報告されたrollback directoryに残します。
プラグインのデータ（McRemoteの認証情報を含む）は書き戻しません。
