# radiko-client（Python）

Rust 版 [radiko-client](https://github.com/yanmakazuki/radiko-client) の Python 移植です。基準にした `main` は `a7eaefa56729161e449a03ab2c0d2872429d8e87` です。対応と、まだ無い機能は [docs/parity.md](docs/parity.md) に分けてあります。

番組表、検索条件、セッション、チャプターの読み取り、HLS の窓、AAC の復号、Linux の音声出力、CLI、TUI の操作を、Rust 版のモジュールの流れに沿って置いています。画面はライブラリを直接の通信口にしません。

この移植だけで Findy のスコアが上がることは保証しません。

## 目次

- [必要なもの](#必要なもの)
- [セットアップ](#セットアップ)
- [保存](#保存)
- [音声](#音声)
- [試験で使うオリジン](#試験で使うオリジン)
- [コマンド](#コマンド)
- [TUI](#tui)
- [テスト方針](#テスト方針)
- [既知の制約](#既知の制約)
- [変更履歴](#変更履歴)
- [アーキテクチャ](docs/architecture.md)
- [設計判断（ADR）](docs/adr/README.md)
- [APIリファレンス](docs/api.md)
- [開発への参加](CONTRIBUTING.md)
- [Rust版との対応](docs/parity.md)
- [ライセンス](#ライセンス)

## 必要なもの

- Python 3.12 以降
- Linux で音を出すとき、または `output-info` で機器を読むとき: ALSA（Debian / Ubuntu は `libasound2t64` または `libasound2`）
- macOS では AudioToolbox の AudioQueue で f32 を出します。この Linux の試験はライブラリが無いときの失敗と、偽の AudioQueue への形式だけです。実機の再生は未検証です

依存は次で固定しています。

- `av==14.2.0`（AAC の復号。PCM は Rust の `rusty_aac` とビット一致しません）
- `numpy==1.26.4`
- `regex==2026.11.7`（書記素）

## セットアップ

```sh
python -m pip install -e ".[dev]"
radiko-client --help
```

開発用の検査:

```sh
ruff format --check src tests
ruff check src tests
python -m mypy src
pytest
```

ホイールを作業ツリーの外で起動する例:

```sh
python -m pip wheel --no-deps -w /tmp/radiko-wheels .
python -m pip install --target /tmp/radiko-prefix /tmp/radiko-wheels/*.whl -r requirements.txt
cd /tmp
/tmp/radiko-prefix/bin/radiko-client --help
/tmp/radiko-prefix/bin/radiko-client inspect-chapters /path/to/tests/fixtures/chapters.json 90 0
```

## 保存

セッションは `session.json`、検索条件は `searches.json` です。パスワードは保存しません。Unix ではディレクトリを `0700`、ファイルを `0600` にします。既定の場所は、Linux が `$XDG_DATA_HOME/radiko-client`（無ければ `~/.local/share/radiko-client`）、macOS が `~/Library/Application Support/jp.radiko-client.radiko-client` です。`--data-dir` で両方をまとめて変えられます。

壊れたファイルは、ログインや保存が成功するまで残します。トークンと署名付き URL はログに出しません。

## 音声

`output-info` は既定の出力を読むだけで、音は出しません。機器が無いときは「既定の音声出力機器がありません」です。`RADIKO_ALSA_LOG=1` のときだけ ALSA の診断を残します。

`RADIKO_NULL_SINK=1` は試験用のプロセス内出力です。Rust 版 CI の PulseAudio null sink に相当しますが、壁時計では待たず、渡したフレーム数だけ進みます。ハードウェアの出力ではありません。

Linux の実出力は libasound の `default` です。macOS は CoreAudio の AudioQueue です。どちらも、この環境では音の出る装置としては確認していません。

レート変換はしません。選ぶ形式は f32、次に i32、次に i16 です。

## 試験で使うオリジン

通常のテストは radiko へ接続しません。番組表だけ、次の環境変数が `http://127.0.0.1:ポート` のとき、そのサーバを読みます。

```sh
RADIKO_CATALOG_ORIGIN=http://127.0.0.1:8080
```

それ以外のホストは拒否します。実アカウントのログインと、実配信の再生は、このリポジトリのテストには入っていません。

## コマンド

Rust 版と同じ名前です。日付を省略した番組表は、日本時間の午前 5 時で区切った当日です。

```sh
radiko-client programs TBS 20261015
radiko-client searches save 生活 TBS 20261014 20261015 --keyword 生活
radiko-client searches show 生活
radiko-client inspect-chapters tests/fixtures/chapters.json 90 0
radiko-client output-info
RADIKO_NULL_SINK=1 radiko-client probe-output --mute 1
radiko-client tui --help
```

`probe-live`、`probe-timefree`、`play-sample`、`play-live`、`play-timefree`、`chapters` は、本番の URL を組み立てて取得します。試験で `RADIKO_LOOPBACK_ORIGIN=http://127.0.0.1:ポート` を付けると、同じパスをそのポートへ載せ替えます。付けないときは `https://radiko.jp` と `https://api.radiko.jp` へ出ます。実アカウントでの成功再生は、このリポジトリでは確認していません。

`RADIKO_NULL_SINK=1` の再生完了は、壁時計でその秒数を待った結果ではありません。

`tui --offline` はヘルプに出さない試験用です。端末で無いときは「対話端末で実行してください」で終了します。

## TUI

`radiko-client tui` で起動します。通常画面の終了は `q`（`Ctrl+C` でも終了）です。引数は `programs` と同じ局・地域・期間・絞り込みを受けます。

| キー | 動作 |
| --- | --- |
| `j` / `k`、`↑` / `↓` | 番組を1つ移動 |
| `PageUp` / `PageDown` | 1画面移動 |
| `Home` / `End` | 先頭 / 末尾へ移動（選択だけでは再生しない） |
| `Enter` | 選択枠を再生（ライブ画面ではライブ） |
| `/` | キーワード編集 |
| `f` | 全条件編集 |
| `b` | 名前付き条件の保存・呼び出し |
| `r` | 新規取得 |
| `Space` | 一時停止 / 再開 |
| `s` | 停止 |
| `←` / `→` | 15秒シーク |
| `+` / `-` | 音量 |
| `g` | 枠内の秒数を指定（タイムフリーのみ） |
| `l` | 選択枠のライブ |
| `v` | ライブ / タイムフリー画面の切替 |
| `c` | 公式AIチャプターを開く |
| `Tab` | 章一覧 / 要約の切替（チャプター画面） |
| `e` | 今回の取得状況・未取得の地域 / 局 / 日 |
| `d` | 出力機器の読み取り専用診断（Esc / d / q で閉じる、終了しない） |
| `n` | 放送中の番組へ移動 |
| `o` | 選択局の条件を編集 |
| `I` / `O` | ログイン / ログアウト |
| `a` | セッション再確認 |
| `?` | 操作一覧 |
| `q` | 終了（通常画面） |
| `Ctrl+C` | 終了 |
| `Esc` | 通知を消す（通常画面） |

`/` や `f` の入力中は、`Enter` で検索を確定し、`Esc` で取り消します。`?` の操作一覧を開いている間も、検索と再生は続きます。入力欄の編集キーと `F2` は `?` の操作一覧を参照してください。

画面は通信を直接持たず、番組表は `radiko.catalog`、再生は `radiko.prepare` 経由で取得します。詳しくは [docs/architecture.md](docs/architecture.md) を参照してください。

## テスト方針

通常のテストは radiko へ接続しません。番組表は fixture と `RADIKO_CATALOG_ORIGIN`、再生系は `RADIKO_LOOPBACK_ORIGIN` のループバックを使います。実アカウントのログインと実配信の再生はテストに含めていません。

実音声は `RADIKO_NULL_SINK=1` のプロセス内出力で確認します。壁時計の再生時間を待つものではなく、渡したフレーム数だけ進みます。

Linux CI を最終根拠にします。macOS は 0 failed、skip 3件（ALSA / coreaudio / Rust未ビルド）です。検証コマンドと CI の構成は [CONTRIBUTING.md](CONTRIBUTING.md) にあります。

## 既知の制約

- Python 3.12 以降が必要です。依存は `av==14.2.0`、`numpy==1.26.4`、`regex==2026.11.7` に固定しています。
- AAC の PCM は Rust 版の `rusty_aac` とビット一致しません。復号は PyAV の libavcodec です。
- 実機の音声出力は未検証です。Linux は ALSA、macOS は AudioQueue を使います。レート変換はしません。
- HE-AAC、fMP4、再生中の再認証は部分対応です。`docs/parity.md` の状態「意図した差」「未実装」を正とします。
- 実アカウントのログインと実配信の成功再生は、このリポジトリでは確認していません。

## 変更履歴

変更の一覧は [CHANGELOG.md](CHANGELOG.md) にあります。

## ライセンス

MIT License です。Rust 版と同じ文面を [LICENSE](LICENSE) に置いています。`tests/fixtures/` は Rust 版の合成データで、認証情報は含みません。
