from partyline_llm.audio import (
    FRAME_BYTES,
    PCMUFrameBuffer,
    pcmu_to_pyvoip_u8,
    pyvoip_u8_to_pcmu,
)


def test_silence_conversion() -> None:
    pyvoip_silence = b"\x80" * FRAME_BYTES
    pcmu = pyvoip_u8_to_pcmu(pyvoip_silence)
    assert pcmu == b"\xff" * FRAME_BYTES
    assert pcmu_to_pyvoip_u8(pcmu) == pyvoip_silence


def test_pcmu_frame_buffer_splits_and_pads() -> None:
    buffer = PCMUFrameBuffer(frame_bytes=4)
    assert buffer.append(b"123") == []
    assert buffer.append(b"456789") == [b"1234", b"5678"]
    assert buffer.flush() == b"9\xff\xff\xff"
    assert buffer.flush() is None

