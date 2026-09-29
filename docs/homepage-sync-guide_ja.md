# ホームページを更新する

公開VPSが配信するホームページの正本は、[mc-remote-knowledge](https://github.com/Naohiro2g/mc-remote-knowledge)の`30-広告宣伝/homepage/`です。コマンドは対象hostで、deployment projectのdirectoryに入って実行します。最初に環境を確かめます。

```sh
cd "$HOME/mc-remote-deployments/<deployment名>"
mcrctl operator check
```

`mcrctl`が見つからない場合は、[PATHが通っていないとき](fresh-host-bootstrap-guide_ja.md#pathが通っていないとき)で戻します。

knowledgeの`main`へ変更がmergeされたら、次を実行します。

```sh
mcrctl homepage sync
mcrctl doctor
```

`homepage sync`は、knowledgeの`main`を一時的にshallow cloneし、配信用directoryを丸ごと入れ替えてから、Caddyだけを作り直します。Minecraft、Scratch、Bridgeは止まりません。deployment lockは変わりません。

配信内容を戻したいときは、knowledge側でrevertしてから、もう一度`homepage sync`を実行します。
