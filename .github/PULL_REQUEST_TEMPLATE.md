<!-- 1 Issue = 1 PR。変更は300行以内に収め、超える場合は Issue を分けます。 -->

Closes #N

## 変えたこと

## 変えてないこと

## 検証コマンドと結果

```sh
ruff format --check src tests
ruff check src tests
python -m mypy src
pytest -q
```

macOS は 0 failed・skip 3件、Linux CI を最終根拠にします。

## リスク
