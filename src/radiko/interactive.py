"""対話再生の操作。無効なシークは今の再生を止めず、新しい要求だけを残す。"""

from __future__ import annotations

from datetime import datetime, timezone

from radiko.stream import parse_time, validate_station


class InteractiveControl:
    def __init__(self, duration: float, mode: str = "timefree", station: str = "TBS"):
        self.duration = duration
        self.mode = mode
        self.station = station
        self.position = 0.0
        self.volume = 25
        self.paused = False
        self.generation = 0
        self.stopped = False
        self.messages: list[str] = []
        self.accepted: list[float] = []
        self.applied = 0
        self.selected: tuple[str, str, datetime, float] | None = None

    def handle(self, line: str) -> None:
        text = line.strip()
        if text in {"q", "stop"}:
            self.stopped = True
            self.generation += 1
            return
        if text == "pause":
            self.paused = True
            self.generation += 1
            self.messages.append("一時停止")
            return
        if text == "resume":
            if self.paused:
                self.paused = False
                if self.mode == "live":
                    self._request(0.0)
                else:
                    self._request(self.position)
            return
        if text == "status":
            state = "一時停止中" if self.paused else "再生中"
            self.messages.append(f"{state} / 音量{self.volume}% / 再生位置: {self.position:.3f}秒")
            return
        if text.startswith("volume "):
            try:
                volume = int(text[7:].strip())
            except ValueError:
                self.messages.append("音量は0〜100で指定してください")
                return
            if not 0 <= volume <= 100:
                self.messages.append("音量は0〜100で指定してください")
                return
            self.volume = volume
            return
        if text in {"+15", "-15"}:
            delta = 15 if text == "+15" else -15
            if self.mode == "live" and delta > 0:
                self.messages.append(
                    "シーク不可: タイムフリーの放送枠内を指定してください（再生を継続）"
                )
                return
            target = max(0.0, self.position + delta)
            self._seek(target)
            return
        if text.startswith("seek "):
            try:
                target = float(text[5:].strip())
            except ValueError:
                self.messages.append("秒数を指定してください")
                return
            self._seek(target)
            return
        if text.startswith("live ") or text.startswith("timefree "):
            parts = text.split()
            if len(parts) < 2:
                self.messages.append("切替不可: 局・日時・開始位置を確認してください")
                return
            try:
                validate_station(parts[1])
            except Exception:
                self.messages.append("切替不可: 局・日時・開始位置を確認してください")
                return
            mode = parts[0]
            offset = 0.0
            now = datetime.now(timezone.utc)
            if mode == "timefree":
                if len(parts) < 3:
                    self.messages.append("切替不可: timefreeには日時が必要です")
                    return
                try:
                    cursor = parse_time(parts[2])
                except Exception:
                    self.messages.append(
                        "切替不可: 日時は日本時間のYYYYMMDDHHmmssで指定してください"
                    )
                    return
                rest = parts[3:]
                if rest:
                    if len(rest) != 2 or rest[0] != "--offset":
                        self.messages.append("切替不可: 日時と--offsetの指定を確認してください")
                        return
                    try:
                        offset = float(rest[1])
                    except ValueError:
                        self.messages.append("切替不可: 位置は0〜86400秒で指定してください")
                        return
                    if not 0 <= offset <= 86400:
                        self.messages.append("切替不可: 位置は0〜86400秒で指定してください")
                        return
                if cursor > now:
                    self.messages.append("切替不可: 放送前のため配信取得できません")
                    return
            else:
                if len(parts) > 2:
                    self.messages.append("切替不可: liveに日時は指定できません")
                    return
                cursor = now
            self.selected = (mode, parts[1], cursor, offset)
            self._request(offset)
            return
        self.messages.append(
            "live 局ID / timefree 局ID YYYYMMDDHHmmss [--offset 秒数] / status / pause / "
            "resume / volume 0〜100 / seek 秒数 / +15 / -15 / q を入力してください"
        )

    def _seek(self, target: float) -> None:
        if self.mode != "timefree" or not 0 <= target < self.duration:
            self.messages.append(
                "シーク不可: タイムフリーの放送枠内を指定してください（再生を継続）"
            )
            return
        self._request(target)

    def _request(self, position: float) -> None:
        self.generation += 1
        self.position = position
        self.accepted.append(position)

    def complete(self, generation: int, position: float) -> bool:
        if generation != self.generation:
            return False
        self.position = position
        return True
