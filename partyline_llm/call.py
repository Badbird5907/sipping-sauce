from __future__ import annotations

from typing import Protocol

from pyVoIP.VoIP import CallState


class AudioCall(Protocol):
    state: CallState

    def read_audio(self, length: int = 160, blocking: bool = True) -> bytes: ...

    def write_audio(self, data: bytes) -> None: ...

    def hangup(self) -> None: ...

