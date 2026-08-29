# 運営者が調整するファイルは全renderer横断でseed-onceにする設計

## 0. 位置づけと状態

- 状態: **確定・全renderer実装済み**（2026-08-30）。
- 対象: `mc_remote_stack.render`が生成する全てのcompose renderer
  （`_compose_v1`〜`_compose_v14`、home-server系・vps-server系の両方）。
- 対象外: legacy YAMLパイプライン（`render_project()`/`_compose()`）。このパイプラインは
  `apply`非対応・`legacy-yaml`として既にNOTES上で対象外と判明済み（2026-07-30時点）で
  あり、稼働中の`apply`経路には使われない。触れていない。
- 適用範囲の但し書き: この文書はStack repo側の**renderer coreの修正**を扱う。実際に
  現行public beta（VPS）へこの新しいrenderer挙動を反映するには、運営者が対象host上で
  `mcrctl deployment update plan/apply`（またはrelease更新手順）を実行する必要がある。
  Stackはそのhostへの実apply権限を持たない（実行境界）。この文書はcode修正の正本であり、
  「いつVPSへ展開するか」は運営者の別判断。

## 1. 問題（運営者からの指摘、2026-08-30）

> configを永続化するとは自己否定以外の意味がない。dockerを利用する意味は、初期設定が
> 環境に左右されず簡単確実、運用面でもメリットがあること。コンソールに直接アクセス
> できないことが唯一のマイナスと考えていたが。通常のサーバー運営上、ユーザーによる
> 調整のために存在するファイルは全て編集可能になっている必要があるのは自明。paperだと
> paper.jar以外全てでは？

これは`home-server@6`（`compose@14`）に限った問題ではなく、**全renderer共通**の
実装だった。`server.properties`と`plugins/McRemote/config.yml`は、`compose@1`
（`_compose_v1`、home-server系の基盤）と`compose@2`（`_compose_v2`、vps-server系の
基盤、現行public beta `compose@13`もこれを継承）の両方で、itzg/docker-minecraft-server
imageの`COPY_CONFIG_DEST=/data`＋`SYNC_SKIP_NEWER_IN_DESTINATION=false`により、
**container再起動のたびにStackが生成したtemplateへ強制的に巻き戻される**。運営者が
直接編集しても次の再起動で消える。

## 2. 原則

Dockerの価値は「初期設定が環境非依存で簡単確実、運用上の再現性」にある。**永続的に
特定の状態を強制し続けること（＝運営者の調整を毎回上書きする）はこの価値の否定**で
ある。区別すべき2種類のファイルがある。

| 種別 | 例 | 扱い |
| --- | --- | --- |
| immutableな配布artifact | `paper.jar`、pluginの`.jar`本体、OCI image | exact digest／SHA-256でpin。運営者は改変しない・改変できない |
| 運営者調整用のconfigファイル | `server.properties`、`plugins/<Plugin>/config.yml`全般（McRemote含む） | Stackは**良いdefaultで初回seedする**。以降は運営者の編集を尊重し、Stackは強制的に上書きしない |

「良いテンプレから始まる」こと自体は有用（初回セットアップの簡便さ）だが、
「そのテンプレを恒久的に強制し続ける」ことは別の話であり、後者だけが問題だった。

## 3. 根本原因（SSOT照合、knowledge commit `fa9f08a353e1`）

`00-hub/DECISIONS_ja.md` `2026-07-04-03`によれば、`auth.enforcement`トグルは
b2開発時、Python clientが認証実装で先行しScratch clientが未対応だった過渡期に、
3リポ同時atomic flipを避け非同期着地を許すために導入された。**リリース既定は
`enforcement ON`（トグルONがb2完了ゲート）**であり、`false`は「認証機構が原理的に
対応できない/困難なclientが混在する」「認証機構自体に不具合がありbypassが要る」と
いった狭い用途にのみ意味を持つ、通常運用や開発過程で必須ではない機能である。

SSOT上、「config.yml全体をDockerで強制ロックしてこのデフォルトを守る」という決定は
どこにも存在しない。このrepoの2026-08-05 NOTES「b2 auth enforcement deployment
correction」が記す実装対応——McRemote plugin同梱JARの出荷時defaultが`false`のまま
公開されていた問題を訂正するため、Stackが独自にconfig.ymlを生成してmountした——は
本来「`auth.enforcement`一箇所の訂正」が目的だった。しかし採用した実装手段
（config.yml全体を生成しCOPY_CONFIG_DESTで強制再適用）が、意図せず「config.yml全体を
運営者が一切触れない」という広すぎる制約へ一般化してしまっていた。**設計時の勘違いが
実装に残り続けた事例**。

## 4. 安全網は保持される

`doctor.py`の`doctor_auth_not_enforced`check（`_auth_enforcement_required`と
`hello_probe`の組合せ、`doctor.py:1216-1221`）は、config fileの中身ではなく
**実際に稼働しているMcRemote serverへtoken無しhelloを送り、protocol応答が
`auth-required`であることを確認する**。したがって運営者が万一
`auth.enforcement: false`へ変更しても、`mcrctl doctor`はlive挙動から検知して
fail closedになる。「ファイルを編集不能にする」ことでしか守れない性質の保証では
なかった。config.ymlをseed-onceにしても、この安全網は無傷で残る。

## 5. 修正内容

`COPY_CONFIG_DEST=/data`と組で使う`SYNC_SKIP_NEWER_IN_DESTINATION`を、
全renderer基盤で`"false"`から`"true"`へ変更した。

- `_compose_v1`（`render.py:518`）——home-server系（`compose@1`/`@5`/`@6`が継承。
  `@6`＝`compose@14`は§本文書適用前の2026-08-30に個別実装済みだったものを、
  今回この横断修正へ統合）
- `_compose_v2`（`render.py:848`）——vps-server系（`compose@2`〜`@13`が継承、
  現行public beta含む）

挙動：

- volumeが空の初回boot: 従来通りStackが生成したtemplateが`/data`へcopyされる。
- 通常のcontainer再起動（`mcrctl render`を挟まない）: `/config`側（render
  出力）のmtimeは直近renderの時刻のまま変わらないため、運営者がcontainer内で
  直接編集した`server.properties`／`plugins/<Plugin>/config.yml`
  （編集時刻の方が新しい）は**残る**。
- 明示的な`mcrctl render`＋`apply`／`mcrctl deployment update apply`: 新しい
  templateのmtimeがsourceとして更新されるので、運営者が意図して更新をpushした
  時だけ新しいdefaultsが反映される（destinationより新しければ上書き、destination
  の方が新しければ温存——itzg imageの標準比較ロジック）。

## 6. 適用範囲外にしたもの

- legacy YAMLパイプライン（`render_project()`、`render.py:2569`付近の
  `SYNC_SKIP_NEWER_IN_DESTINATION`）は変更していない。稼働中の`apply`経路では
  使われないため（2026-07-30 NOTES確認済み）。
- McRemote plugin自身のruntime reload能力（in-game/console commandでの
  無停止設定変更）は本文書の範囲外。今回の修正は「Docker再起動をまたいで
  設定が生きる」ことを保証するもので、「サーバーを止めずに設定を切り替えられる」
  こととは別の話。後者が要るかは別途McRemote plugin側の設計判断
  （AGENTS.mdのSSOTプロトコルに従いknowledge repoを読んでから着手）。

## 7. 展開手順（production VPSを含む、人間実行）

コード修正はrepoへ反映済みだが、**現行public betaへ即座に反映されるわけではない**。
既存deploymentへ反映するには、通常のrelease更新経路（`mcrctl deployment update
plan/apply`）でrenderer revisionが上がったexact lockへ更新し、対象host上でapplyする
必要がある。この実apply操作はStackの実行境界外（対象hostへのアクセスを持つ運営者が
行う）。
