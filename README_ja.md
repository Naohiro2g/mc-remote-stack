# mc-remote-stack

[English here.](README.md)

`mc-remote-stack` は、McRemoteサーバーを再現可能な形で設置・運用するためのパッケージです。
人が編集する一つの `mc-remote.yml` から、検証済みでdigestを固定したruntime設定を生成します。

このプロジェクトは、次のものと意図的に分離しています。

- `mc-remote-knowledge`: アーキテクチャと意思決定のSSOT（Single Source of Truth、信頼できる唯一の情報源）
- `server-runbook`: 新しいhostのbootstrapと運用知識
- deployment project: instance固有のdesired state（望ましい状態）とlockデータ

## 開発

```sh
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run mcrctl --help
```

## 最初のvertical slice

```sh
uv run mcrctl init ./deployment --profile official-vps
uv run mcrctl validate --project ./deployment
uv run mcrctl repo check --project ./deployment
uv run mcrctl plan --project ./deployment
uv run mcrctl accept-eula --project ./deployment --yes
uv run mcrctl render --project ./deployment --output ./deployment/generated
```

`plan` は、EULAへの同意と変更不能なartifact identityがそろうまで停止します。対象にはOCI image、Paper、plugin JARに加え、
ホームページのversionとarchive SHA-256も含まれます。未解決のselectorを暗黙に本番deploymentへ変換することはありません。
`render` は同じgateを通過した後にだけ、Compose、Caddy、Scratch runtime、Bridge route、ServerBackupの設定を生成します。
この最初のvertical sliceには、生成したファイルをhostへ適用する機能はまだ実装されていません。

初期化したlockは、意図的に特定versionへ固定していません。profileが選ぶものはtopologyとpolicyであり、MinecraftやMcRemoteのreleaseでは
ありません。このため既存サーバーを移行するときは、回収したartifactを固定しつつ、インフラ移行と同時にMcRemoteのupgradeを強制されずに
済みます。

## 暗号化したoff-host backup転送

最初のtransfer adapterは、ServerBackupのarchiveを公開age recipientで暗号化してから、明示的なFTPS sessionを開始します。
証明書の検証を必須とし、data connectionを保護し、passive modeを使います。一時的なremote filenameでuploadした後にrenameし、
最終的なremote sizeを検証します。`--verify-download` を付けると、remoteの暗号文をdownloadし、そのSHA-256も比較します。
平文と暗号化済みのlocal fileはqueueに残り、transfer処理では削除しません。

```sh
uv run mcrctl backup transfer /backup/outbox/backup.zip \
  --project ./deployment \
  --verify-download
```

FTPS passwordは `secret://backup_ftps_password` として参照し、`mcrctl secret set` で保存します。deployment projectには保存しません。
VPSしか持たない利用者は、既存のSSH/SFTP経路を使ってoutboxのartifactをdownloadし、別の場所へuploadすることもできます。
このパッケージはVPSへFTP daemonをinstallしません。VPS内にだけ存在するsnapshotはlocal recovery stateであり、off-host backupでは
ありません。

秘密値を含む既存のサーバー全体のrecovery pointを、展開せずに調査するには次を実行します。

```sh
uv run mcrctl archive inspect /path/to/backup.zip --json
```

結果にはarchiveのSHA-256、ZIP CRCの検査結果、合計size、region数、rootにあるserver JARのidentity、使用中の
`plugins/*.jar` のSHA-256が含まれます。Paperのremap cacheやplugin libraryも数えますが、使用中のpluginとして誤って報告しません。
plugin設定の内容は表示しません。

deployment lockで指定したPaperとplugin JARだけをrecovery archiveから取り込むには、次を実行します。

```sh
uv run mcrctl artifact import-archive /path/to/backup.zip --project ./deployment
```

このcommandはarchive全体のSHA-256を検証し、指定した各memberが一つだけ存在することを確認し、streamしながら各artifactの
SHA-256を検証します。その後、対象のJARだけをcontent-addressed local storeへ保存します。world dataやplugin設定は展開しません。
`MC_REMOTE_ARTIFACT_HOME` でlocal storeの場所を変更でき、`--store` で明示的なSHA-256 store directoryを指定できます。

生成されたMinecraft Compose設定は、lockしたPaper JARを `PAPER_CUSTOM_JAR` でmountし、lockした各pluginをimageの
`/plugins` attach pointへread-onlyでmountします。起動時には、`/data/plugins` 直下にある古いJARだけを削除してから、lockした
一式を同期します。pluginのdata directoryは残しつつ、名前が変わって残った古いJARは除去されます。Paper defaultのruntime downloadは
無効で、生成済み設定を `/config` から同期します。

official profileは、`mc-remote.com` と `www.mc-remote.com` をCaddyのstatic siteとして生成します。ホームページの内容は
`/var/lib/mc-remote/homepage/sha256/<sha256>` からread-onlyでmountし、証明書、private key、ACME stateはCaddyの独立した
`/data` volumeに保存します。レンタルサーバーから回収したarchiveでは、`source_archive` provenanceを使い、取得元のSHA-256、
source root、意図的に除外したhost固有fileを記録できます。
