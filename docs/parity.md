# 移植の対応表

基準にした Rust 版 `radiko-client` の `origin/main` は、作業開始時に取得した次の完全 SHA です。以降このポートはここから動かしていません。

`a7eaefa56729161e449a03ab2c0d2872429d8e87`

このコミットは「終了時刻の放送日の翌日05:00を共有する (#82)」で、`main` に含まれています。未マージのコミットは入れていません。

状態の意味:

- **対応**: 同じ入出力・同じ失敗・同じ保存形式を、このリポジトリのテストで見ている
- **意図した差**: 言語と実行環境の差として残す。バグとしての先送りではない
- **未実装**: まだ Python 側に無い。完了とは扱わない

## ライブラリ

| 領域 | 状態 | 内容 |
| --- | --- | --- |
| 放送日 05:00 JST、状態、1〜30日 | 対応 | `tests/test_contracts.py` |
| タイムフリーの翌日 05:00 を基準にした 7 日 / 30 日、契約不足と配信対象外 | 対応 | Rust の期限表と同じ期待文字列 |
| 検索条件の AND/OR、未分類、キーワード | 対応 | fixture の番組で確認 |
| `session.json` / `searches.json` | 対応 | serde_json の実バイトと比較。0700/0600、破損は残す、シンボリックリンクは拒否 |
| 番組表 XML / 局一覧 | 対応 | 同梱 fixture。壊れた枠は数えて除外 |
| チャプター JSON | 対応 | `inspect-chapters` の行と件数 |
| HLS の少し古い窓と、タイムフリーの窓喪失 | 対応 | 通し番号の判定だけ |
| MP4 箱のサンプル数上限 | 対応 | 復号の前に拒否 |
| ADTS の CRC・複数 block 拒否と AAC 復号 | 意図した差 | 復号は PyAV（libavcodec）。PCM は rusty_aac とビット一致しない |
| fragmented MP4 の連続再生、`PROGRAM-DATE-TIME` と `tfdt` の 2ms 照合 | 未実装 | 箱の上限とコンテナ名の判別だけ。probe の本線は ADTS |
| 再生中の 401 再認証、ライブの追いつき全文、5 分再接続 | 未実装 | 準備中の 401 と取り消しは返す。再生中の差し替えはまだ |
| 会員ログイン手順 | 部分 | POST は再試行しない。契約フラグと auth1/auth2 はローカルの本番パスで確認。実アカウントは未使用 |
| 配信解決 | 部分 | 番組 JSON、エリア、黒枠、配信先 XML、プレイリストのクエリまで本番 URL で接続した。署名 URL の全例外はまだ |

## CLI

| コマンド | 状態 | 内容 |
| --- | --- | --- |
| `--help`、不明な引数、終了コード 1 | 対応 | 標準エラーは `エラー: ` で始まる |
| `--data-dir` | 対応 | 1 回だけ。空や `--` では失敗 |
| `session` / `logout` / `login` | 対応 | ファイルが無ければディレクトリを作らない。`login` は非 TTY を拒否し、パスワードをエラーに出さない |
| `searches` | 対応 | 保存・表示・復元・削除。壊れたファイルは上書きしない |
| `inspect-chapters` | 対応 | fixture の行 |
| `programs` の引数 | 対応 | 不正な局・日付・30日超・分類は通信前に失敗 |
| `programs` の取得 | 部分 | `RADIKO_CATALOG_ORIGIN` が `http://127.0.0.1` のときだけ。fixture サーバで JSON と本文を確認。既定の `https://radiko.jp` はテストしていない |
| `output-info` / `probe-output` | 部分 | 引数範囲は一致。機器が無い Linux は「既定の音声出力機器がありません」。`RADIKO_NULL_SINK=1` は壁時計では待たない単体出力 |
| `probe-live` / `probe-timefree` / `play-sample` / `play-live` / `play-timefree` / `chapters` | 部分 | 本番 URL を組み立て、`RADIKO_LOOPBACK_ORIGIN` のときだけ同じパスを `127.0.0.1` へ載せる。会員確認、auth1/auth2、番組 JSON、局一覧、配信先 XML、HLS、ADTS、取り消しをこの経路で確認した。変数が無いときは本番ホストへ出る。実 radiko の成功再生は未試験 |
| `--interactive` | 未実装 | 非 TTY ではフラグを無視して一本の再生に進む |
| 終了コード 141 | 意図した差 | 定数と分類はある。閉じたパイプのプロセス試験はまだ |

## TUI

| 操作 | 状態 | 内容 |
| --- | --- | --- |
| `--help` から `--offline` を隠す、非 TTY を拒否 | 対応 | |
| 書記素の1行編集、表示幅、コンパクト（高さ 29 以下） | 対応 | ヘッドレスな状態機械 |
| キーワード入力中の未確定件数 | 対応 | 一覧は取り直さない。Enter で世代が進む |
| 通知の重要度、隠れた警告、Esc は終了しない | 対応 | 色の描画は `NO_COLOR` の有無だけ |
| 放送中への移動、放送前は再生しない | 対応 | |
| オフライン fixture | 部分 | 1局1番組を組み立てる。端末の生入力ループはあるが、PTY の全画面比較はしていない |
| Ratatui のセル、機器診断の非同期、チャプタージャンプ、ログイン欄 | 未実装 | 状態機械の試験はセル一致ではない |

## 意図した実行環境の差

- AAC は `rusty_aac` ではなく PyAV 14.2.0 の libavcodec。拒否する ADTS と、HE-AAC 宣言なのに SBR が無いときの失敗は合わせた。サンプル値は合わせない。
- 出力の優先は f32、i32、i16。レート変換はしない。Linux は libasound の `default`。macOS の CoreAudio は未実装。
- `RADIKO_NULL_SINK=1` は PulseAudio の null sink ではなく、フレーム数だけ進むプロセス内出力。壁時計では待たない。
- `RADIKO_ALSA_LOG=1` のときだけ ALSA の診断を残す。
- `RADIKO_CATALOG_ORIGIN` と `RADIKO_AUTH_ORIGIN` は `http://127.0.0.1:ポート` だけを許す。Rust のデバッグビルドにある抜け道に相当し、リリースバイナリとの差として残す。

## オフライン CLI の突き合わせ

`a7eaefa56729161e449a03ab2c0d2872429d8e87` を Rust 1.85.0 でビルドした `radiko-client` と、同じ引数の Python プロセスを比べた。ヘルプ、不正な引数、`session` / `logout` / `login`、`searches` の保存・表示・一覧・復元・削除、`inspect-chapters` は、終了コードと標準出力と標準エラーが一致した。通信が要るコマンドと、TUI のセルは比べていない。バイナリが無い CI では `tests/test_rust_cli_compare.py` を飛ばす。

## 次の区切り

fragmented MP4 の連続復号、再生中の 401 再認証、ライブの追いつき表示、macOS の CoreAudio、TUI の実画面。TUI の状態機械の試験はセル一致ではない。
