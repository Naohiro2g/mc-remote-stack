# ケータリング型サーバー構築の検証ロードマップ

## 0. 現在地と目的

現在はb2 release後、b3の内容を決める前に、中期的なdeployment基盤を整える段階である。
b3の機能内容をこの作業へ混ぜず、次の3環境が揃った時点をb3開発の開始条件とする。

1. VPS上の公開beta
2. home server上の非公開alpha
3. ケータリング型とは独立したplain dev

物理host名、IP、provider、private inventoryはこのrepoへ置かない。ここでは公開可能なrole、
order contract、検証順、完了条件だけを扱う。

## 1. 用語と作業線の分離

### ケータリング型

preset catalog / preset registry、order、lock、content-addressed artifact、plan / apply境界を使い、
既存Ubuntu hostとclean install hostの両方へ同じ構成機構を適用するサーバー構築方式。
公式環境も利用者向け公開機構と同じmodule、artifact、profile / preset機構で構成する。

### ケータリングパッケージ

ノートPC等のclosed environmentで、clean Ubuntu installから利用者が構築できる配布・bootstrap面。
ケータリング型を利用するが、VPS公開betaやb3開始条件と同じ作業線にはしない。

### plain dev

破壊的変更と短いiterationを優先する開発環境。ケータリング型の成立確認に使わず、
ケータリング型へ合わせるための制約を追加しない。

### 詳細runbook

実例を反映しながら改善する独立作業線。安全な最小手順は各検証に必要だが、
万能runbookの完成をb3開始条件にしない。

## 2. 検証順

| 順序 | 環境 | exposure | 目的 | 終了条件 |
| --- | --- | --- | --- | --- |
| 1 | home private beta | `isolated` | ケータリング型の最初のlive bootstrap | clean / existing Ubuntuの両経路でexact resolve、render、apply、doctorが通る |
| 2 | home private alpha | `isolated`から開始 | alpha orderと独立data境界の検証 | betaと別project / volume / worldでresolve、render、apply、doctorが通る |
| 3 | VPS public beta | `public` | provider制約下の新方式と現行公開betaからの移行 | rollback可能な移行後、公開endpointとruntime smokeが通る |

home private betaは検証用であり、最終的に維持する3環境の一つではない。VPS公開betaへの移行後も
home private alphaとplain devを独立して維持する。

## 3. 現在の到達点

- home private beta:
  - 既存Ubuntu hostからのbootstrapを確認済み
  - clean Ubuntu Server hostからのbootstrapを確認済み
  - exact artifact、canonical render、bootstrap apply、healthy doctor、tokenなしprotocol helloを確認済み
- home private alpha: 未検証
- VPS public betaのケータリング型移行: 未検証
- plain dev: 本ロードマップの実装対象外
- ケータリングパッケージのノートPC検証: 並行作業
- 詳細runbook: 並行作業

## 4. home private alphaの最初のorder contract

最初のalpha検証は次を明示する。

| axis | value |
| --- | --- |
| profile | `home-server@2` |
| preset | `mcremote-paper@2` |
| channel | `alpha` |
| exposure | `isolated` |
| purpose | `integration` |
| renderer | `compose@1` |

`mcremote-paper@2`は、b3内容を先取りせず、最初はb2で確認済みのexact artifactを固定して
alpha order / deployment機構そのものを検証する。b3開発開始後、実験対象componentを採用する場合は
tagやmoving selectorでなく、選択時のexact source commit、build recipe、output digestを固定する。
実験対象でないcomponentは既知artifactへ固定する。

alphaはbetaと別のproject root、deployment name、environment identity、runtime volume、
world identityを使う。directory名や物理host名からaxisを推測せず、betaのorder / lockをcopy、
rename、暗黙変換しない。

このpreset revisionはlive alpha evidence取得前は`unverified`とする。初回applyでは理由付きorder
acknowledgement、one-shot flag、plan / lock reviewを要求する。実機結果はsanitized draftとして
knowledge ownerへhandoffし、正式evidence着地後に別compatibility recordでratifyする。

## 5. b3開始gate

次をすべて満たしたら、deployment基盤を理由にb3内容決定を止めない。

- VPS公開betaがケータリング型のexact order / lockから稼働している
- home非公開alphaが独立project / volume / worldで稼働している
- plain devが利用可能である
- 各環境の役割、停止許容、data寿命、秘密境界が区別されている
- 公開beta移行のrollbackまたは復旧手順が実地確認されている

ケータリングパッケージの追加検証、対象host上agentの限定実験、runbookの網羅化は並行継続でき、
それだけを理由にb3開始を止めない。
