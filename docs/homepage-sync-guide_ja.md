# ホームページを更新する

公開VPSが配信するホームページの正本は、
[mc-remote-knowledge](https://github.com/Naohiro2g/mc-remote-knowledge)の`30-広告宣伝/homepage/`です。
コマンドは対象hostで実行します。`MC_REMOTE_STACK`はStack checkout、`MC_REMOTE_PROJECT`は
deployment projectのpathです（[VPS更新の手順](public-vps-bootstrap-guide_ja.md)と同じ値）。

knowledgeの`main`へ変更がmergeされたら、次を実行します。

```sh
uv run --project "$MC_REMOTE_STACK" mcrctl homepage sync --project "$MC_REMOTE_PROJECT"
uv run --project "$MC_REMOTE_STACK" mcrctl doctor --project "$MC_REMOTE_PROJECT"
```

`homepage sync`は、knowledgeの`main`を一時的にshallow cloneし、配信用directoryを丸ごと入れ替えてから、
Caddyだけを作り直します。Minecraft、Scratch、Bridgeは止まりません。deployment lockは変わりません。

配信内容を戻したいときは、knowledge側でrevertしてから、もう一度`homepage sync`を実行します。
