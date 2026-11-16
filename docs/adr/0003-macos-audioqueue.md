# 0003: macOS の音声出力に AudioQueue を使う

- 状態: 採用済み
- 日付: 2026-11-16
- 関連: [アーキテクチャ](../architecture.md) / [Rust 版との対応](../parity.md)

## 背景

macOS では Linux の ALSA が使えません。Python 側の音声出力を OS ごとに分け、Linux は libasound、macOS は AudioToolbox の AudioQueue を使います。実機の再生はまだ検証していません。

## 決定

macOS では AudioQueue を使い、インターリーブの f32 を出します。`output.open_stream` は `host_backend()` が `coreaudio` のとき `coreaudio.Stream` を返します。Linux の ALSA は f32 / i32 / i16 を選べますが、macOS の AudioQueue は f32 のみを扱います。

`coreaudio.Stream` は `AudioQueueNewOutput` でキューを作り、完了コールバックを `_callbacks` に保持します。コールバックは `AudioQueueStop` まで生かし、`AudioQueueDispose` で手放します。書き込みは1秒以内のバッファに分けます。未完了バッファがあるまま完了通知が3秒（一度も完了が無ければ活動開始から3.25秒）止まれば `stalled` とします。定数は [coreaudio.py](../../src/radiko/coreaudio.py) の `CALLBACK_WAIT_SECONDS` と `NO_CALLBACK_SECONDS` です。

## 代替案

- PortAudio を使う。追加の依存とネイティブライブラリが必要になり、固定依存の方針に合わないため採りません。
- SDL2 を使う。追加の依存とイベントループが必要で、この CLI には大きすぎるため採りません。
- ffmpeg / ffplay の CLI を使う。外部プロセスになり、出力の型・進捗・停止をこのアプリの型で扱えないため採りません。
- Linux と同じ ALSA を使う。macOS に libasound が無いため採りません。

## 結果

- macOS の出力は f32 の mono / stereo のみです。デバイス名は制御文字を落とし、256文字で切ります。
- コールバックの寿命、停滞検知、バッファ再利用、mono から stereo への変換は、偽の AudioToolbox を使ったテストで確認します。実機の再生は未検証です。macOS CI のテストは偽の AudioToolbox を使い、実音声を出しません。
- 実装は [coreaudio.py](../../src/radiko/coreaudio.py) と [output.py](../../src/radiko/output.py) にあります。テストは [test_coreaudio_backend.py](../../tests/test_coreaudio_backend.py) です。
- 差と未検証事項は [parity.md](../parity.md) と [README.md](../../README.md) の「既知の制約」に記録しています。
