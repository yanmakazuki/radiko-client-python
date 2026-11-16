# 0002: テスト音声にプロセス内 null sink を使う

- 状態: 採用済み
- 日付: 2026-11-16
- 関連: [アーキテクチャ](../architecture.md) / [Rust 版との対応](../parity.md)

## 背景

再生経路のテストには音声出力が必要ですが、CI に実機の音声デバイスはありません。Rust 版の CI は PulseAudio の null sink を使います。Python 側でも、ブロックせず、再生位置と停止・一時停止を再現できる出力が必要です。

## 決定

`RADIKO_NULL_SINK=1` のとき、`output.open_stream` は `_NullStream` を返します。`_NullStream.write` は実時間を待たず、`rate // 50`（最低1）フレームずつ `self.frames` を進めます。停止・一時停止のイベントを見て途中で打ち切ります。実際の音声デバイスは開きません。

これはプロセス内のテスト出力です。Rust 版の CI が使う PulseAudio の null sink とは別物です。ハードウェアの時間、バッファリング、デバイスのエラーは再現しません。

## 代替案

- PulseAudio の null sink を使う。CI に PulseAudio と設定が必要になり、テストの前提が増えるため採りません。
- ALSA の `null` プラグインを使う。libasound とデバイス設定に依存し、OS ごとの差が残るため採りません。
- `/dev/null` に書く。フレーム数、停止、一時停止、進捗を観測できないため採りません。
- 実機の音声出力を使う。CI で再現できず、時間もかかるため採りません。

## 結果

- `RADIKO_NULL_SINK=1` の再生は、壁時計の経過ではなく渡したフレーム数で完了します。`OutputSummary.frames` で出力済みフレームを確認できます。
- 実音声ではありません。音質、遅延、アンダーラン、デバイス復帰の検証には使えません。これらは実機の課題として残ります。
- 実装は [output.py](../../src/radiko/output.py) にあります。テストは [test_local_io.py](../../tests/test_local_io.py) と [test_playback_loopback.py](../../tests/test_playback_loopback.py) です。
- この差は [parity.md](../parity.md) の「意図した実行環境の差」に記録しています。
