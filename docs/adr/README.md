# ADR（設計判断の記録）

主要な技術判断を1件1ファイルで残します。全体の構成は [アーキテクチャ](../architecture.md)、Rust 版との対応は [Rust 版との対応](../parity.md) を参照してください。

| 番号 | タイトル | 状態 | 日付 | 要約 | ファイル |
| --- | --- | --- | --- | --- | --- |
| 0001 | AAC の復号に PyAV 14.2.0 を使う | 採用済み（Accepted） | 2026-11-16 | Rust の `rusty_aac` と PCM はビット一致しない意図した差 | [0001-use-pyav.md](0001-use-pyav.md) |
| 0002 | テスト音声にプロセス内 null sink を使う | 採用済み（Accepted） | 2026-11-16 | `RADIKO_NULL_SINK=1` は壁時計で待たないテスト出力 | [0002-in-process-null-sink.md](0002-in-process-null-sink.md) |
| 0003 | macOS の音声出力に AudioQueue を使う | 採用済み（Accepted） | 2026-11-16 | f32 の AudioQueue。実機の再生は未検証 | [0003-macos-audioqueue.md](0003-macos-audioqueue.md) |

## 新しいADRの追加方法

1. `docs/adr/NNNN-short-title.md` を連番で作ります。
2. 「背景 / 決定 / 代替案 / 結果」の4見出しで書きます。
3. この索引に追加し、状態が変わったら「状態」を更新します。
