# APIリファレンス

`src/radiko` と `src/radiko_client` の主要な公開APIを、モジュールごとに並べます。`__all__` は無いため、先頭 `_` のないトップレベル定義を公開APIとして扱い、利用者向けの主要なものを抜粋しています。内部関数・私有クラスは載せていません。説明は各docstringの先頭1文です。自動生成ツールは使わず、実装とdocstringから手で選んでいます。

モジュール構成は [architecture.md](architecture.md)、Rust 版との対応は [parity.md](parity.md) を参照してください。`radiko.fixtures` は試験専用のため載せていません。

## radiko

### [radiko.domain](../src/radiko/domain.py)

放送枠、放送日、検索条件。

- [`broadcast_day`](../src/radiko/domain.py#L42): 入力のタイムゾーンによらず、日本時間の午前5時を放送日の境界にする。
- [`broadcast_status`](../src/radiko/domain.py#L70): now が開始前・放送中・終了後のどれかを返す。
- [`ProgramKey`](../src/radiko/domain.py#L96): 局IDと開始時刻で番組を一意にするキー。
- [`Classification`](../src/radiko/domain.py#L108): 番組や出演者の分類。
- [`Program`](../src/radiko/domain.py#L116): 番組表の1番組。
- [`Station`](../src/radiko/domain.py#L142): 放送局。
- [`BroadcastRange`](../src/radiko/domain.py#L150): 検索対象の放送日範囲。
- [`Chapter`](../src/radiko/domain.py#L179): チャプター1件。
- [`ChapterSet`](../src/radiko/domain.py#L190): チャプターの集合。
- [`Filter`](../src/radiko/domain.py#L218): 検索条件。
- [`Scope`](../src/radiko/domain.py#L427): 検索範囲。
- [`SearchCriteria`](../src/radiko/domain.py#L470): 検索条件の一式。
- [`criteria_from_file`](../src/radiko/domain.py#L530): 保存用の辞書から SearchCriteria を復元する。

### [radiko.catalog](../src/radiko/catalog.py)

キャッシュしない局一覧と日別番組表。

- [`CatalogError`](../src/radiko/catalog.py#L36): 番組表・局一覧の取得と解析の失敗。
- [`ProgramList`](../src/radiko/catalog.py#L143): 1局・1放送日の番組表。
- [`parse_programs`](../src/radiko/catalog.py#L154): 番組表 XML を検証し、`<prog>` を Program にして放送日順で返す。
- [`parse_stations`](../src/radiko/catalog.py#L248): 局一覧 XML を検証し、Station の並びを返す。
- [`parse_endpoints`](../src/radiko/catalog.py#L284): 配信先 XML から、モードと地域に合う `playlist_create_url` を返す。
- [`CatalogClient`](../src/radiko/catalog.py#L479): fetch 関数で XML を取得し、局一覧と番組表を解析するクライアント。
- [`acquire_range`](../src/radiko/catalog.py#L545): 地域の並びから局一覧を並列取得し、続けて全員分の番組表を取得する。
- [`acquire_stations`](../src/radiko/catalog.py#L596): 局IDの並びから全員分の番組表を並列取得する。

### [radiko.auth](../src/radiko/auth.py)

会員ログインと契約フラグ。

- [`AuthError`](../src/radiko/auth.py#L20): 認証の失敗。
- [`MemberClient`](../src/radiko/auth.py#L82): 会員ログインとセッション確認。
- [`parse_membership`](../src/radiko/auth.py#L199): 会員情報の JSON を解析する。
- [`authenticate`](../src/radiko/auth.py#L253): 配信認証の唯一の実装。

### [radiko.prepare](../src/radiko/prepare.py)

配信の準備。

- [`ResolvedProgram`](../src/radiko/prepare.py#L49): API から解決した1番組。
- [`ProbeReport`](../src/radiko/prepare.py#L70): probe の結果。
- [`PlayReport`](../src/radiko/prepare.py#L110): 再生ループの結果。
- [`ChapterView`](../src/radiko/prepare.py#L143): チャプター取得の表示用ビュー。
- [`prepare`](../src/radiko/prepare.py#L304): 再生に必要な番組・認証・エリア・配信先 URL を解決する。
- [`inspect_hls`](../src/radiko/prepare.py#L402): マスター/メディアプレイリストをたどり、先頭セグメントを probe する。
- [`probe`](../src/radiko/prepare.py#L700): 番組を解決して配信先ごとに HLS を試し、最初に成功した ProbeReport を返す。
- [`play_station`](../src/radiko/prepare.py#L955): 再生を実行して PlayReport を返す公開ラッパー。
- [`inspect_remote_chapters`](../src/radiko/prepare.py#L1561): 番組のチャプターを取得して ChapterView を返す。

### [radiko.http_client](../src/radiko/http_client.py)

制限付き HTTP。

- [`Reply`](../src/radiko/http_client.py#L23): HTTP 応答。
- [`get`](../src/radiko/http_client.py#L130): GET。
- [`post_form`](../src/radiko/http_client.py#L385): ログイン POST。
- [`catalog_fetch`](../src/radiko/http_client.py#L408): 番組表を取得して gzip を解凍する。
- [`stream_get`](../src/radiko/http_client.py#L464): 本番 URL を載せ替えて取得する。

### [radiko.reauth](../src/radiko/reauth.py)

再生中の HTTP 401 だけを、保存済みセッションで一度取り直す。

- [`ReauthState`](../src/radiko/reauth.py#L17): 再生中の401をエピソードごとに1回だけ再認証する状態。

### [radiko.hls](../src/radiko/hls.py)

HLS メディアプレイリストの窓。

- [`MediaPlaylist`](../src/radiko/hls.py#L31): メディアプレイリストの窓。
- [`HlsCursor`](../src/radiko/hls.py#L59): 再生中のプレイリスト窓。
- [`parse_media_playlist`](../src/radiko/hls.py#L273): メディアプレイリストを解析して検証する。
- [`segment_schedule`](../src/radiko/hls.py#L193): EXTINF と PROGRAM-DATE-TIME を1パスで畳み込み、添字参照できる表にする。
- [`live_join_index`](../src/radiko/hls.py#L252): 終端から target duration の3倍以内に始まる最も古いセグメント。
- [`parse_master_codec`](../src/radiko/hls.py#L361): マスタープレイリストの CODECS を持つ最初の STREAM-INF から CODECS を返す。

### [radiko.media](../src/radiko/media.py)

MP4 の箱の上限、ADTS の境界、AAC の復号。

- [`AacError`](../src/radiko/media.py#L27): AAC の解析・復号の失敗。
- [`DecodedPcm`](../src/radiko/media.py#L84): 復号済み PCM と要約。
- [`split_adts`](../src/radiko/media.py#L133): ADTS をフレームへ分割する。
- [`AdtsDecoder`](../src/radiko/media.py#L240): セグメントをまたいで形式を覚え、失敗したら状態を捨てる。
- [`validate_mp4`](../src/radiko/media.py#L340): MP4 の箱構造と上限を検証する。

### [radiko.fragmented](../src/radiko/fragmented.py)

単一 AAC トラックの fragmented MP4。

- [`FragmentPcm`](../src/radiko/fragmented.py#L20): fMP4 1フラグメントの復号結果。
- [`FragmentedAacDecoder`](../src/radiko/fragmented.py#L264): 単一 AAC トラックの fragmented MP4 デコーダ。

### [radiko.output](../src/radiko/output.py)

音声出力。

- [`OutputStream`](../src/radiko/output.py#L116): 1回開いた出力を複数のチャンクで使い回す。
- [`open_stream`](../src/radiko/output.py#L261): バックエンドを選んでストリームを開く。
- [`play_samples`](../src/radiko/output.py#L311): インターリーブされた f32 を出す。
- [`probe_tone`](../src/radiko/output.py#L277): 440Hz・振幅0.2の生成音を1〜10秒出す。
- [`probe_silent`](../src/radiko/output.py#L290): 無音を1〜1800秒出す。
- [`diagnose_default_output`](../src/radiko/output.py#L52): 既定機器の f32 mono / stereo を読む。
- [`inspect_default_output`](../src/radiko/output.py#L93): 既定出力機器の対応形式を調べる。

### [radiko.audio](../src/radiko/audio.py)

音声出力の共通型。

- [`OutputError`](../src/radiko/audio.py#L8): 音声出力の失敗。
- [`OutputInfo`](../src/radiko/audio.py#L34): 実際に開いた出力の情報。
- [`OutputCapability`](../src/radiko/audio.py#L45): バックエンドが申告する能力。
- [`OutputSummary`](../src/radiko/audio.py#L56): 出力ストリームの累計。

### [radiko.storage](../src/radiko/storage.py)

session.json と searches.json。

- [`MemberSession`](../src/radiko/storage.py#L22): 再利用できるセッショントークンだけを持つ。
- [`Membership`](../src/radiko/storage.py#L56): 会員の聴取可否。
- [`StorageError`](../src/radiko/storage.py#L75): session.json の読み書き失敗。
- [`SearchError`](../src/radiko/storage.py#L105): searches.json の読み書き失敗。
- [`default_data_dir`](../src/radiko/storage.py#L159): directories 6 の data_local_dir と同じ場所。
- [`SessionStore`](../src/radiko/storage.py#L194): session.json の読み書き。
- [`Library`](../src/radiko/storage.py#L304): searches.json の中身。
- [`SearchStore`](../src/radiko/storage.py#L315): searches.json の読み書き。

### [radiko.stream](../src/radiko/stream.py)

配信の時刻、局 ID、タイムフリー期限。

- [`ProbeError`](../src/radiko/stream.py#L12): 配信の検証・取得の失敗。
- [`SlotProgram`](../src/radiko/stream.py#L129): 番組表の1枠。
- [`eligible`](../src/radiko/stream.py#L139): 再生時点の可否を判定する。
- [`area_contract`](../src/radiko/stream.py#L166): エリア外の契約判定。
- [`availability`](../src/radiko/stream.py#L179): 番組情報のタイムフリー可否を見る。
- [`deadline_pair`](../src/radiko/stream.py#L201): 無料期限（放送日の翌日05:00+7日）と有料期限（+30日）を RFC3339 で返す。

### [radiko.chapters](../src/radiko/chapters.py)

チャプター JSON の正規化。

- [`ChapterError`](../src/radiko/chapters.py#L13): チャプター JSON の失敗。
- [`parse_chapters`](../src/radiko/chapters.py#L32): チャプター JSON を検証して ChapterSet にする。
- [`chapters_from_json`](../src/radiko/chapters.py#L46): デコード済みの JSON から正規化する。

### [radiko.interactive](../src/radiko/interactive.py)

対話再生の操作。

- [`InteractiveControl`](../src/radiko/interactive.py#L10): 対話再生の状態。

### [radiko.text](../src/radiko/text.py)

表示幅と書記素。

- [`graphemes`](../src/radiko/text.py#L35): 拡張書記素クラスタ。
- [`display_width`](../src/radiko/text.py#L42): 表示幅0の文字（結合文字・書式文字・C0とDEL）は0、全角と全幅は2、それ以外のクラスタは1。
- [`clean_text`](../src/radiko/text.py#L84): 空白は一つに畳み、Cc の制御文字は捨てる。

### [radiko.timing](../src/radiko/timing.py)

断片の時刻照合と、再生開始後の再接続待ち。

- [`accept_fragment`](../src/radiko/timing.py#L18): PCM 長と EXTINF、tfdt の差と PROGRAM-DATE-TIME の差は 2ms 以内。
- [`trim_pcm`](../src/radiko/timing.py#L47): 番組時刻から目標までを切り捨てる。
- [`ReconnectBudget`](../src/radiko/timing.py#L77): 再生が一度始まったあとの通信失敗だけを、1 秒から倍々で最大 5 分待つ。

### [radiko.urls](../src/radiko/urls.py)

本番の配信先を組み立て、試験のときだけ 127.0.0.1 へ載せ替える。

- [`trusted`](../src/radiko/urls.py#L18): 配信先として信頼する URL か。
- [`loopback_origin`](../src/radiko/urls.py#L33): RADIKO_LOOPBACK_ORIGIN を検証して origin を返す。
- [`transport_url`](../src/radiko/urls.py#L54): 本番 URL を検証してから、ループバックが設定されていればそこへ載せる。
- [`append_query`](../src/radiko/urls.py#L68): 既存クエリを保ったまま pairs を追加する。

### [radiko.dynamic_library](../src/radiko/dynamic_library.py)

OS の共有ライブラリを解決し、パスごとに1つだけ読み込む。

- [`find_library`](../src/radiko/dynamic_library.py#L14): `ctypes.util.find_library` の結果を名前ごとにキャッシュして返す。
- [`open_library`](../src/radiko/dynamic_library.py#L28): パスごとに CDLL を1つだけ読み込み、以後は同じものを返す。

### [radiko.coreaudio](../src/radiko/coreaudio.py)

macOS の CoreAudio。

- [`inspect`](../src/radiko/coreaudio.py#L125): 既定出力機器の名前と f32 mono/stereo の対応を返す。
- [`Stream`](../src/radiko/coreaudio.py#L143): 1つの AudioQueue を複数チャンクで使い回す。

## radiko_client

### [radiko_client.cli](../src/radiko_client/cli.py)

引数、終了コード、各サブコマンド。

- [`main`](../src/radiko_client/cli.py#L101): CLI の入口。
- [`run`](../src/radiko_client/cli.py#L129): `--data-dir` を分離し、サブコマンドへ振り分ける。

### [radiko_client.programs](../src/radiko_client/programs.py)

programs の引数解析と検索条件。

- [`ProgramRequest`](../src/radiko_client/programs.py#L18): programs の解析結果。
- [`parse_program_request`](../src/radiko_client/programs.py#L38): programs の引数を解析する。
- [`build_criteria`](../src/radiko_client/programs.py#L121): 引数を解析して検索条件にする。

### [radiko_client.tui](../src/radiko_client/tui.py)

検索と再生の TUI。

- [`Screen`](../src/radiko_client/tui.py#L488): TUI の全状態。
- [`FilterForm`](../src/radiko_client/tui.py#L104): 全条件。
- [`LineEditor`](../src/radiko_client/tui.py#L257): 1行入力の編集状態。
- [`NoticeLevel`](../src/radiko_client/tui.py#L233): 通知の重要度。
- [`Notice`](../src/radiko_client/tui.py#L247): フッターに出す通知1件。
- [`run`](../src/radiko_client/tui.py#L2045): TUI の入口。

### [radiko_client.io](../src/radiko_client/io.py)

CLI の標準出力と標準エラー。

- [`ClosedPipe`](../src/radiko_client/io.py#L10): 標準出力のパイプが閉じられたことを表す。
- [`WriteFailed`](../src/radiko_client/io.py#L18): 標準出力への書き込み失敗。
- [`stdout_ln`](../src/radiko_client/io.py#L38): 1行書いて flush する。
- [`stderr_ln`](../src/radiko_client/io.py#L50): stderr に1行書いて flush する。
