# mc-remote-stack

McRemote server の再現可能な公開 deployment / operations package と、現行実装に一致する public runbook のリポジトリです。

## McRemote SSOT

McRemote 固有の設計判断の正本は GitHub 上の `Naohiro2g/mc-remote-knowledge` です。

McRemote 固有文脈に依存する作業に入る前に、agent はその SSOT リポジトリの関連文書を
必ず読んでください。対象には architecture / protocol / deployment / contributor workflow /
learning design、および McRemote 固有の判断理由に依存する挙動変更が含まれます。

最初に、knowledge リポのリモート `main` から最新の dev agent runtime protocol だけを取得して
指示として読んでください。取得元ファイル全体を会話へ出力してはいけません。

```bash
protocol_source="$(mktemp)"
knowledge_commit="$(gh api repos/Naohiro2g/mc-remote-knowledge/commits/main -q .sha)"
gh api "repos/Naohiro2g/mc-remote-knowledge/contents/00-hub/dev-repo-protocol_ja.md?ref=$knowledge_commit" \
  -q .content | base64 -d > "$protocol_source"
if [ "$(grep -Fxc '<!-- BEGIN: DEV-AGENT-RUNTIME -->' "$protocol_source")" -ne 1 ] || \
   [ "$(grep -Fxc '<!-- END: DEV-AGENT-RUNTIME -->' "$protocol_source")" -ne 1 ]; then
  echo "dev agent runtime marker missing or duplicated" >&2
  exit 1
fi
printf 'knowledge commit: %s\n' "$knowledge_commit"
awk '/^<!-- BEGIN: DEV-AGENT-RUNTIME -->$/{reading=1;next} \
     /^<!-- END: DEV-AGENT-RUNTIME -->$/{reading=0} \
     reading' "$protocol_source"
```

SSOT リポジトリへアクセスできない場合は、作業を止め、その旨を明示してください。この
リポジトリ単体、assistant memory、過去会話、ローカル推論から欠けた文脈を補完してはいけません。
SSOT にアクセスできるまで、McRemote 固有文脈に依存する設計判断や実装を進めないでください。

このファイルは SSOT を複製しません。複製はドリフトを生みます。

- このリポの関連スポーク: `10-protocol/`, `11-plugin/`, `13-scratch-client/`, `14-evidence/`

## このリポ固有の指示

Add tests before fixing bugs.

変更を提出する前に、次を実行してください。

```sh
uv sync --extra dev
uv run pytest
uv run ruff check .
```
