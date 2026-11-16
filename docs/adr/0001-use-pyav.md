# 0001: AAC の復号に PyAV 14.2.0 を使う

- 状態: 採用済み
- 日付: 2026-11-16
- 関連: [アーキテクチャ](../architecture.md) / [Rust 版との対応](../parity.md)

## 背景

Python 移植では、ADTS の AAC と fragmented MP4 の AAC を PCM へ復号する必要があります。Rust 版は `rusty_aac` を使いますが、Python 標準ライブラリには AAC デコーダがありません。依存は `pyproject.toml` で固定し、CI と利用者の環境で同じものを使う方針です。

## 決定

AAC の復号に PyAV 14.2.0 を使います。`pyproject.toml` の `av==14.2.0` で固定します。

ADTS は `media._decode_adts` が `av.open(..., format="aac")` で開きます。fMP4 は `fragmented.FragmentedAacDecoder._decode` が `av.CodecContext.create("aac", "r")` で復号します。どちらも出力は float32 の PCM です。

Rust 版の `rusty_aac` と同じ PCM ビット列にはしません。この差は [parity.md](../parity.md) の「意図した差」に記録しています。

## 代替案

- pure Python の AAC デコーダを実装する。ADTS、SBR、チャンネル構成まで自前で持つ必要があり、維持できる範囲を超えるため採りません。
- ffmpeg の CLI を呼ぶ。プロセス起動、パイプ、エラーコードの変換が増え、再生経路に外部プロセスが入るため採りません。
- Rust 版の `rusty_aac` を FFI で呼ぶ。ビルドと配布に Rust の成果物が必要になり、この移植の依存を増やすため採りません。

## 結果

- AAC の復号は PyAV 14.2.0 の libavcodec に任せ、ADTS と fMP4 で同じデコーダを使います。
- PCM は float32 で、Rust 版の `rusty_aac` とビット一致しません。比較はサンプル値ではなく、拒否するヘッダー・箱・上限・エラーコードで行います。
- 復号の境界は [media.py](../../src/radiko/media.py) と [fragmented.py](../../src/radiko/fragmented.py) にあります。ADTS の復号は [test_tui_player.py](../../tests/test_tui_player.py)、再生経路は [test_playback_loopback.py](../../tests/test_playback_loopback.py) で確認します。
- `av==14.2.0` を上げるときは、[parity.md](../parity.md) の差とテストを再確認します。
