# 開発への参加

`radiko-client-python` の開発環境、検証、PR の粒度をまとめます。モジュール構成は [docs/architecture.md](docs/architecture.md)、Rust 版との対応は [docs/parity.md](docs/parity.md) を参照してください。

## 必要な環境

- Python 3.12 以降
- `uv`（仮想環境と依存の導入に使います）
- Linux で音を出す場合: ALSA（Debian / Ubuntu は `libasound2t64` または `libasound2`）
- macOS で音を出す場合: AudioToolbox（OS に含まれます）
- Rust 版との CLI 比較を行う場合だけ: Rust 1.85.0

`uv` が `PATH` に無いときは、先に通します。

```sh
export PATH="$HOME/.local/bin:$PATH"
```

## セットアップ

```sh
git clone https://github.com/yanmakazuki/radiko-client-python.git
cd radiko-client-python
uv venv --python 3.12 .venv
uv pip install -p .venv/bin/python -e ".[dev]"
source .venv/bin/activate
radiko-client --help
```

依存は `pyproject.toml` で固定しています。`av==14.2.0` は AAC の復号、`numpy==1.26.4` は PCM、`regex==2026.11.7` は書記素に使います。

## 検証

変更したら、次の4コマンドを実行します。

```sh
ruff format --check src tests
ruff check src tests
python -m mypy src
pytest -q
```

- `ruff format --check`: 整形の差分が無いこと。
- `ruff check`: Lint（`E`, `F`, `I`, `W`）。行長は 100 です。
- `python -m mypy src`: 型検査。設定は `pyproject.toml` の `[tool.mypy]` です。
- `pytest -q`: テスト。通常のテストは radiko へ接続しません。

`pyproject.toml` の `addopts = "-q"` と重なるため、`pytest -q` は合格件数の要約を省略します。件数まで見たいときは `pytest` を使います。

カバレッジも見るときは、CI と同じコマンドを使います。

```sh
pytest --cov --cov-report=term-missing
```

`pyproject.toml` の `fail_under = 75` を下回ると失敗します。

## CI

GitHub Actions は3ジョブです。

1. **Ubuntu checks**: `ruff format --check` / `ruff check` / `mypy` / `pytest --cov` / ホイールをツリー外で起動する確認。
2. **macOS checks**: `ruff format --check` / `ruff check` / `mypy` / `pytest`。OS 依存の3件は skip されます。
3. **Rust比較**: Rust 1.85.0 で固定 SHA の Rust 版をビルドし、`tests/test_rust_cli_compare.py` を実行します。

Linux CI の結果を最終根拠にします。macOS は 0 failed、skip 3件です。skip の内訳は、ALSA が Linux 専用（`test_local_io.py`）、macOS では欠落ライブラリの経路を確認できない（`test_coreaudio_backend.py`）、Rust のバイナリをこのジョブではビルドしない（`test_rust_cli_compare.py`）です。

## Issue と PR

- 1 Issue = 1 ブランチ = 1 PR にします。
- 変更行数は 300 行を上限にします。超える場合は Issue を分けます。
- ブランチ名は `fix/...`、`refactor/...`、`docs/...` のように内容を表す名前にします。
- 既存テストを弱めません。振る舞いを変えるときは、先にテストを追加します。
- レビュー後の追加修正は、force push ではなく追加コミットで積みます。
- マージはレビュー後に行います。PR の作者は自分でマージしません。

PR 本文には次の見出しを置きます。

```markdown
Closes #N

## 変えたこと

## 変えてないこと

## 検証結果

## リスク
```

`検証結果` には、上の4コマンドの結果と、必要なら CI のジョブ結果を貼ります。macOS の skip が既知の3件だけなら、その旨を書きます。

## Rust 版とのパリティ

Rust 版 `yanmakazuki/radiko-client` の `a7eaefa56729161e449a03ab2c0d2872429d8e87` を基準にします。CLI の入出力、終了コード、保存形式、エラーの契約を壊さないでください。

言語と実行環境による意図した差（AAC の PCM がビット一致しない、null sink がプロセス内、など）は [docs/parity.md](docs/parity.md) にあります。この文書を正とし、Rust 版リポジトリは参照だけにします。

## 秘密情報と実アカウント

- トークン、パスワード、署名付き URL を Issue、PR、ログ、コミットに書かないでください。
- 実アカウントでのログインや、実配信の再生をテストに使いません。
- テストは fixture、`RADIKO_CATALOG_ORIGIN` / `RADIKO_LOOPBACK_ORIGIN` のループバック、`RADIKO_NULL_SINK=1` のプロセス内出力を使います。
- 認証情報を含むファイルをコミットしないでください。`session.json` と `searches.json` はデータディレクトリに置きます。

## コードの方針

- Python 3.12 の機能を使ってよいですが、対応バージョンは下げません。
- 新しい依存は、Issue に明記がある場合だけ追加します。
- `src/radiko` は端末に依存させません。CLI / TUI の都合は `src/radiko_client` に置きます。
- 関数内 import は使いません。循環を避けるときはモジュール境界を見直します。
- docstring とコメントは日本語、識別子は英語です。
