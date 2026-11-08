# radiko-client（Python）

Rust 版 [radiko-client](https://github.com/yanmakazuki/radiko-client) の Python 移植です。基準にした `main` は `a7eaefa56729161e449a03ab2c0d2872429d8e87` です。対応と、まだ無い機能は [docs/parity.md](docs/parity.md) に分けてあります。

番組表、検索条件、セッション、チャプターの読み取り、HLS の窓、AAC の復号、Linux の音声出力、CLI、TUI の操作を、Rust 版のモジュールの流れに沿って置いています。画面はライブラリを直接の通信口にしません。

この移植だけで Findy のスコアが上がることは保証しません。

## 必要なもの

- Python 3.12 以降
- Linux で音を出すとき、または `output-info` で機器を読むとき: ALSA（Debian / Ubuntu は `libasound2t64` または `libasound2`）
- macOS の CoreAudio 出力は未実装です。引数の検査と、`RADIKO_NULL_SINK=1` のプロセス内出力は動きます

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

## ライセンス

MIT License です。Rust 版と同じ文面を [LICENSE](LICENSE) に置いています。`tests/fixtures/` は Rust 版の合成データで、認証情報は含みません。
