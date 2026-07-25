# Wake-on-LAN optional operation field note

## 1. 位置づけ

Wake-on-LAN（WoL）は、準24時間運用のserverを使わない時間だけstandbyまたはpoweroffし、
現地操作なしで復帰させるための重要なoperator機能である。電力、騒音、hardware寿命と、
必要時にすぐ利用できることの両立に価値がある。

ただし、WoLを一般bootstrapの必須機能、profile capability、compatibility条件、
release / b3開始gateにはしない。firmware、NIC、driver、OS、power state、給電、
sender topologyへの依存が大きく、利用者の手元で再現できないhardware条件を
McRemoteの成立条件として強制しないためである。検証のためにclean installを強制しない方針と
同じく、重要性と標準要件化を分ける。

この文書は万能な設定手順ではなく、再現可能な検証境界とsanitizedな実例を示すfield noteである。
VPS公開betaの常時運用要件や、停電・OS hangからの復旧保証には読み替えない。

## 2. packet sender

公開例の第一選択CLIは `wakeonlan` とする。Ubuntuでは同名packageとして提供され、
複数MAC、destination address、portを指定できる。

- `wakeonlan`はoperator workstationまたは既存のsender hostで使うoptional toolである。
- target hostのbootstrap依存にはせず、利用者へpersistent installを強制しない。
- packageを追加できない既存Ubuntu hostでは、Python標準libraryによる102-byte magic packet送信を
  reference / controlとして使える。
- sender toolが終了status 0でもtarget復帰の証明にはならない。到達不能、復帰、boot ID、
  network、service healthを別に確認する。
- toolとPython controlを比較する回では、同じdestination broadcast、UDP port、target MACを使う。

`wakeonlan`の既定destinationはlimited broadcast `255.255.255.255`だが、canonical exampleでは
対象LANのdirected broadcastを明示する。

```sh
wakeonlan -i <target-subnet-broadcast> <target-mac>
```

これにより、VPN、複数NIC、Wi-Fi / Ethernet、container network等があるsenderで
対象linkを監査しやすくなる。target IPへのunicastは、sleep / poweroff中にARP entryが
失われる可能性があるため既定にしない。directed broadcastもrouter越えを保証するものではなく、
最初は同一subnet内で検証する。

参考:

- [Ubuntu `wakeonlan` manpage](https://manpages.ubuntu.com/manpages/jammy/man1/wakeonlan.1.html)
- [Ubuntu 24.04 `wakeonlan` package](https://packages.ubuntu.com/source/noble/wakeonlan)

個人host名、MAC address、private IP、SSH portをpublic repoへ置かない。個人用aliasや
`wakeonlan -f`のinventory fileはGit外またはprivate backstageへ置く。aliasを公開例へ転記せず、
field noteではplaceholderを使う。

## 3. 有線WoLとWoWLANを分ける

有線NICのWoLと、内蔵Wi-FiのWake on WLAN（WoWLAN）は別機能である。複数MACへpacketを送れたこと、
WindowsでWi-Fi復帰したこと、有線WoLがPASSしたことから、Ubuntu WoWLANの成立を推測しない。

USB Wi-Fi / USB Ethernetでは、adapter firmware、driver、USB remote wake、host controller、
firmware給電のすべてが関係する。USB portがstandby中も給電されるだけではPASSとしない。

Wi-Fi-to-Ethernet bridgeまたはstandalone hardware APを使う場合、PC側は通常の有線WoLとして
検証できる。ただしbridgeがbroadcastを透過すること、offline時のDHCP / DNS、client isolation、
設定restoreは別に実機確認する。

## 4. live validation contract

失敗時にKVMまたは物理電源で復旧できるときだけ実施する。2台を相互sender / targetにする場合も
同時に停止せず、常にsender 1台と別の回復経路を残す。

1. sender / target、default LAN interface、subnet、directed broadcast、tool versionを確認する。
2. targetのboot ID、running service数、healthを取得する。
3. senderからtargetのdirected broadcastへrouteされることを確認する。
4. 人間が対象とpower stateを確認してからdeep sleepまたはpoweroffを実行する。
5. deep sleepでは複数回の到達不能と自然復帰しないことを確認してからpacketを送る。
6. poweroffではSSH不能だけを完了条件にしない。shutdown中にnetworkが先に停止するhostがあるため、
   monitor、KVM、LED等で完全消灯を人間が確認した後にpacketを送る。
7. SSH復帰、boot ID、network、対象serviceのhealthを確認する。
8. deep sleepではboot ID維持、poweroffではboot ID更新を期待する。

packet送信後に復帰しない場合は、同じpacketを無制限に再送してPASSへ寄せない。送信時点が
power transitionの途中でなかったか、destination、sender route、firmware、NIC、OS profileを
切り分ける。検証用packetと緊急回復用packetは記録上区別する。

## 5. 2026-07-25 Ubuntu相互検証

異なる2台のdesktop hardwareを同一LAN上の相互sender / targetとして検証した。
両機でPython 3.12.3と `wakeonlan 0.41`を使用し、target subnetのdirected broadcastへ送信した。
private host名、MAC、IP、boot ID実値は記録しない。

| target | sender | method | power state | SSH復帰 | service health復帰 | result |
| --- | --- | --- | --- | ---: | ---: | --- |
| hardware A | hardware B | Python | deep sleep | 11秒 | 13秒 | PASS |
| hardware A | hardware B | `wakeonlan` | deep sleep | 10秒 | 11秒 | PASS |
| hardware A | hardware B | Python | poweroff | 28秒 | 57秒 | PASS |
| hardware A | hardware B | `wakeonlan` | poweroff | 27秒 | 57秒 | PASS |
| hardware B | hardware A | Python | deep sleep | 8秒 | 9秒 | PASS |
| hardware B | hardware A | `wakeonlan` | deep sleep | 8秒 | 9秒 | PASS |
| hardware B | hardware A | Python | poweroff | 32秒 | 55秒 | PASS |
| hardware B | hardware A | `wakeonlan` | poweroff | 29秒 | 52秒 | PASS |

各deep sleepでboot ID維持、各poweroffでboot ID更新を確認した。hardware Aでは1 service、
hardware Bでは独立した2 servicesが元のhealthy状態へ戻った。

hardware Bの最初のpoweroff試行では、3回のSSH不能後に送ったpacketが完全消灯より早く、
180秒以内に復帰しなかった。完全消灯を人間確認してから再送すると復帰したため、
strict caseには数えず、上記contractへ二段confirmationを追加して再試験した。

この結果が主張するのは、上記2台、同一LAN、2 sender実装、tested power stateでの復帰と
service healthだけである。macOS、package version差、別OS、別subnet、router越え、WoWLAN、
hibernate、hybrid sleep、USB adapter、停電復旧、watchdog、backup / restoreは未確認である。

本検証は人間のpower操作とLED / monitor確認を含む `live-human` evidenceとして正式採用された。
knowledge commit `4b8ab4b6e173053e4c9a167011d6ed0c8ae4bd1c`で
決定 `2026-07-25-08`、record、sanitized artifacts、INDEXへの着地を確認済みである。

- [decision](https://github.com/Naohiro2g/mc-remote-knowledge/blob/4b8ab4b6e173053e4c9a167011d6ed0c8ae4bd1c/00-hub/DECISIONS_ja.md)
- [formal record](https://github.com/Naohiro2g/mc-remote-knowledge/blob/4b8ab4b6e173053e4c9a167011d6ed0c8ae4bd1c/14-evidence/records/2026-07-25-ubuntu-desktop-wol-mutual-live-human_ja.md)
- [sanitized artifacts](https://github.com/Naohiro2g/mc-remote-knowledge/tree/4b8ab4b6e173053e4c9a167011d6ed0c8ae4bd1c/14-evidence/artifacts/2026-07-25-ubuntu-desktop-wol-mutual-live-human)

## 6. 外部技術記事

hardware依存が大きいことは、技術発信を避ける理由にはしない。Qiita、Zenn、note、
DEV Community等では、成功例だけでなく、成立条件、失敗条件、切り分け、復旧境界を含む
hardware-specific field reportとして積極的に公開できる。

記事には可能な範囲で次を含める。

- hardware classとfirmware設定
- NIC / Wi-Fi chipset、driver
- OS、kernel、NetworkManager / networkd
- sender tool / version、sender topology、destination broadcastの選び方
- tested power stateと、完全停止をどう確認したか
- boot ID、SSH、service healthによるPASS判定
- PASSしなかった試行と、未確認範囲

記事はpublic runbookやcompatibility contractの代わりではない。MAC、private IP、private host名、
SSH設定、credential、raw logを除き、読者が自分のhardwareへ一般化してよい範囲を明示する。
