from __future__ import annotations

import audioop


SAMPLE_RATE = 8000
FRAME_MS = 20
FRAME_BYTES = SAMPLE_RATE * FRAME_MS // 1000
PYVOIP_SILENCE = b"\x80"
PCMU_SILENCE = b"\xff"


def pyvoip_u8_to_pcmu(audio: bytes) -> bytes:
    """Convert PyVoIP unsigned 8-bit linear PCM into G.711 mu-law."""
    signed_pcm = audioop.bias(audio, 1, -128)
    return audioop.lin2ulaw(signed_pcm, 1)


def pcmu_to_pyvoip_u8(audio: bytes) -> bytes:
    """Convert G.711 mu-law into PyVoIP unsigned 8-bit linear PCM."""
    signed_pcm = audioop.ulaw2lin(audio, 1)
    return audioop.bias(signed_pcm, 1, 128)


class PCMUFrameBuffer:
    """Split arbitrary PCMU chunks into 20 ms RTP-sized frames."""

    def __init__(self, frame_bytes: int = FRAME_BYTES) -> None:
        self.frame_bytes = frame_bytes
        self._buffer = bytearray()

    def append(self, chunk: bytes) -> list[bytes]:
        self._buffer.extend(chunk)
        frames: list[bytes] = []
        while len(self._buffer) >= self.frame_bytes:
            frames.append(bytes(self._buffer[: self.frame_bytes]))
            del self._buffer[: self.frame_bytes]
        return frames

    def flush(self) -> bytes | None:
        if not self._buffer:
            return None
        frame = bytes(self._buffer).ljust(self.frame_bytes, PCMU_SILENCE)
        self._buffer.clear()
        return frame

    def clear(self) -> None:
        self._buffer.clear()

