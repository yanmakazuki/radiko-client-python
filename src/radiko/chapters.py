"""チャプター JSON の正規化。対応付けと聴取可否はここでは決めない。"""

from __future__ import annotations

import json
import unicodedata

from radiko.domain import Chapter, ChapterSet

MAX_CHAPTER_BYTES = 1024 * 1024


class ChapterError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(str(self))

    def __str__(self) -> str:
        return {
            "large": "チャプター応答のサイズ上限を超えました",
            "json": "チャプターJSONを解析できません",
            "array": "chapters配列がありません",
            "timeline": "放送枠の長さまたは時刻補正が不正です",
            "none": "有効なチャプターがありません（不正データ）",
        }[self.code]


def parse_chapters(data: bytes, slot_seconds: float, offset_seconds: float) -> ChapterSet:
    if len(data) > MAX_CHAPTER_BYTES:
        raise ChapterError("large")
    _check_timeline(slot_seconds, offset_seconds)
    try:
        root = json.loads(data.decode())
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ChapterError("json") from error
    return chapters_from_json(root, slot_seconds, offset_seconds)


def chapters_from_json(root, slot_seconds: float, offset_seconds: float) -> ChapterSet:
    """デコード済みの JSON から正規化する。サイズ上限は呼び出し側が確認する。"""
    _check_timeline(slot_seconds, offset_seconds)
    items = root.get("chapters") if isinstance(root, dict) else None
    if not isinstance(items, list):
        raise ChapterError("array")
    result = ChapterSet([], 0, 0)
    for source_index, item in enumerate(items):
        if not isinstance(item, dict):
            result.invalid_count += 1
            continue
        if item.get("isMusic") is True:
            result.music_count += 1
            continue
        if "isMusic" in item and not isinstance(item.get("isMusic"), bool):
            result.invalid_count += 1
            continue
        start = item.get("fromSec")
        end = item.get("toSec")
        if (
            not isinstance(start, (int, float))
            or not isinstance(end, (int, float))
            or isinstance(start, bool)
            or isinstance(end, bool)
        ):
            result.invalid_count += 1
            continue
        start_f = float(start) + offset_seconds
        end_f = float(end) + offset_seconds
        if (
            float(start) < 0
            or float(end) <= float(start)
            or start_f != start_f
            or end_f != end_f
            or abs(start_f) == float("inf")
            or abs(end_f) == float("inf")
            or start_f < 0
            or end_f <= start_f
            or end_f > slot_seconds
        ):
            result.invalid_count += 1
            continue
        result.chapters.append(
            Chapter(
                title=_display(item.get("titleLong")) or _display(item.get("title")) or "無題",
                summary=_display(item.get("summaryLong")) or "",
                start=start_f,
                end=end_f,
                source_index=source_index,
            )
        )
    result.chapters.sort(key=lambda chapter: chapter.start)
    if not result.chapters and result.invalid_count:
        raise ChapterError("none")
    return result


def _check_timeline(slot_seconds: float, offset_seconds: float) -> None:
    if slot_seconds <= 0 or offset_seconds != offset_seconds or abs(offset_seconds) == float("inf"):
        raise ChapterError("timeline")


def _display(value) -> str | None:
    if not isinstance(value, str):
        return None
    text = "".join(char for char in value if unicodedata.category(char) != "Cc").strip()
    return text or None
