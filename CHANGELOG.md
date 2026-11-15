# Changelog

このプロジェクトの主な変更を記録します。形式は [Keep a Changelog](https://keepachangelog.com/ja/1.1.0/) に従います。

## [0.1.0] - 2026-11-15

### 追加

- Rust 版 `radiko-client` の `a7eaefa56729161e449a03ab2c0d2872429d8e87` を基準にした Python 3.12 移植を追加 (#1)
- TUI の起動時復元・条件編集・名前付き条件・再生コマンドを Rust 基準に追加 (#22)
- CI に mypy の型検査を追加し、`src` の型エラーを解消 (#55)
- macOS の CI ジョブを追加し、OS 依存テスト3件の失敗を解消 (#57)
- CI にカバレッジ計測を追加し、下限75%を強制 (#58)
- `_play_station` 分割前の characterization test（現状の振る舞いを固定するテスト）を追加 (#60)
- `domain.py` の公開 API に日本語 docstring を追加 (#67)
- `docs/architecture.md`、`CONTRIBUTING.md`、README の目次を追加 (#69)
- README に TUI・テスト方針・既知の制約の節を追加 (#70)

### 変更

- 音声出力ストリームを常駐化し、PCM を numpy 化 (#11)
- TUI の番組表取得を4並列化し、HTTP Keep-Alive を統合 (#12)
- 番組表 XML を単一パス化し、TUI 幅計算の計算量を削減。MP4 / ADTS の境界コピーも削減 (#13)
- 局切替の非再帰化、`[0]` 参照渡しの廃止、import 整理とデッドコード除去 (#14)
- 配信認証・再生エンジン・テスト用データの境界を整理 (#24)
- HLS の番組時刻・不連続を1パスで前計算し、チャプターの二重デコードを解消 (#33)
- CoreAudio / ALSA のライブラリ解決をキャッシュし、ADTS / fMP4 の境界コピーを削減 (#34)
- TUI 一覧の絞り込み結果をキャッシュし、文字幅計算に ASCII 高速経路を追加 (#36)
- 番組表の子要素抽出を1パス化し、テキスト出力からスナップショット生成を削除 (#37)
- `fragmented._read` を tfhd / tfdt / trun の解析関数に分割 (#62)
- `_play_station` から復号・トリム・limit 判定を `_decode_segment` へ抽出 (#63)
- `Screen.handle` を移動系と一覧操作に分割 (#64)
- `InteractiveControl.handle` をコマンド別ハンドラに分割 (#65)
- `prepare._sample` を取得・復号・レポート組み立てに分割 (#66)
- `_find` / `_open_library` / `_attr` の重複定義を共通化 (#68)

### 修正

- 配信解決の回復経路と fMP4 境界を Rust 基準に揃える (#21)
- CoreAudio の停滞検知・デバイス名・ALSA XRUN を Rust 基準に揃える (#23)
- 書式文字の削除後にカーソルを書記素境界へ戻す (#38)
- 全国エリア列挙テストの待機条件を固有エリア数にする (#56)
- 再生開始前のキャンセルで TUI がエラー通知を出すのを修正 (#61)
- README の TUI キー説明を実装に合わせる (#71)
