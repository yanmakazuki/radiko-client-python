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
        if self._handle_stop_pause(text):
            return
        if self._handle_volume(text):
            return
        if self._handle_seek_command(text):
            return
        if self._handle_source(text):
            return
        self.messages.append(
            "live 局ID / timefree 局ID YYYYMMDDHHmmss [--offset 秒数] / status / pause / "
            "resume / volume 0〜100 / seek 秒数 / +15 / -15 / q を入力してください"
        )

    def _handle_stop_pause(self, text: str) -> bool:
        """停止・一時停止・再開・状態表示を処理する。"""
        if text in {"q", "stop"}:
            self.stopped = True
            self.generation += 1
        elif text == "pause":
            self.paused = True
            self.generation += 1
            self.messages.append("一時停止")
        elif text == "resume":
            if self.paused:
                self.paused = False
                if self.mode == "live":
                    self._request(0.0)
                else:
                    self._request(self.position)
        elif text == "status":
            state = "一時停止中" if self.paused else "再生中"
            self.messages.append(f"{state} / 音量{self.volume}% / 再生位置: {self.position:.3f}秒")
        else:
            return False
        return True

    def _handle_volume(self, text: str) -> bool:
        """volume N を処理する。範囲外は案内だけ出す。"""
        if not text.startswith("volume "):
            return False
        try:
            volume = int(text[7:].strip())
        except ValueError:
            self.messages.append("音量は0〜100で指定してください")
            return True
        if not 0 <= volume <= 100:
            self.messages.append("音量は0〜100で指定してください")
            return True
        self.volume = volume
        return True

    def _handle_seek_command(self, text: str) -> bool:
        """+15 / -15 / seek N を処理する。"""
        if text in {"+15", "-15"}:
            delta = 15 if text == "+15" else -15
            if self.mode == "live" and delta > 0:
                self.messages.append(
                    "シーク不可: タイムフリーの放送枠内を指定してください（再生を継続）"
                )
                return True
            target = max(0.0, self.position + delta)
            self._seek(target)
            return True
        if not text.startswith("seek "):
            return False
        try:
            target = float(text[5:].strip())
        except ValueError:
            self.messages.append("秒数を指定してください")
            return True
        self._seek(target)
        return True

    def _handle_source(self, text: str) -> bool:
        """live / timefree の局指定を処理する。"""
        if not (text.startswith("live ") or text.startswith("timefree ")):
            return False
        parts = text.split()
        guidance = "live 局ID / timefree 局ID YYYYMMDDHHmmss [--offset 秒数] を指定してください"

        def reject() -> bool:
            self.messages.append(guidance)
            return True

        if len(parts) < 2:
            return reject()
        try:
            validate_station(parts[1])
        except Exception:
            return reject()
        mode, offset = parts[0], 0.0
        now = datetime.now(timezone.utc)
        if mode == "timefree":
            if len(parts) < 3:
                return reject()
            try:
                cursor = parse_time(parts[2])
            except Exception:
                return reject()
            rest = parts[3:]
            if rest:
                if len(rest) != 2 or rest[0] != "--offset":
                    return reject()
                try:
                    offset = float(rest[1])
                except ValueError:
                    return reject()
                if not 0 <= offset <= 86400:
                    return reject()
            if cursor > now:
                return reject()
        else:
            if len(parts) > 2:
                return reject()
            cursor = now
        self.selected = (mode, parts[1], cursor, offset)
        self._request(offset)
        return True

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
