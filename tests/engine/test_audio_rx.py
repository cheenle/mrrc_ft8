from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from server.engine.audio_rx import (
    DECIMATION,
    DECODER_SAMPLE_RATE,
    RX_SAMPLE_RATE,
    AudioCapture,
    RxConverter,
    UtcRing,
)


def sine(freq_hz: float, n: int, rate: int = RX_SAMPLE_RATE) -> np.ndarray:
    t = np.arange(n) / rate
    return np.sin(2 * np.pi * freq_hz * t).astype(np.float32)


def test_converter_rate_and_block_size_independence() -> None:
    one_shot = RxConverter().push(sine(1_500.0, 48_000))
    blocked = RxConverter()
    parts = [
        blocked.push(sine(1_500.0, 48_000)[start : start + size])
        for start, size in [(0, 4_096), (4_096, 1_000), (5_096, 37), (5_133, 42_867)]
    ]
    streamed = np.concatenate(parts)
    assert abs(one_shot.size - 48_000 // DECIMATION) <= 1
    assert streamed.shape == one_shot.shape
    # Streaming lfilter + phase-consistent decimation is sample-exact.
    assert np.array_equal(streamed, one_shot)


def test_converter_preserves_passband_tone() -> None:
    pcm = RxConverter().push(sine(1_500.0, 48_000))
    spectrum = np.abs(np.fft.rfft(pcm.astype(np.float64)))
    peak_hz = float(np.argmax(spectrum)) * DECODER_SAMPLE_RATE / pcm.size
    assert abs(peak_hz - 1_500.0) < 5.0
    amplitude = 2.0 * spectrum[int(round(1_500.0 * pcm.size / DECODER_SAMPLE_RATE))] / pcm.size
    assert amplitude > 0.85 * 32_767  # FIR passband is essentially flat


def test_converter_rejects_aliased_tone() -> None:
    pcm = RxConverter().push(sine(9_000.0, 48_000))
    # Skip the startup transient; steady-state stopband attenuation ≳ 53 dB.
    assert np.max(np.abs(pcm[200:])) < 500


def test_converter_clips_to_int16() -> None:
    pcm = RxConverter().push(np.full(48_000, 2.0, dtype=np.float32))
    assert pcm.max() == 32_767
    pcm = RxConverter().push(np.full(48_000, -2.0, dtype=np.float32))
    assert pcm.min() == -32_768


def test_ring_serves_exact_contiguous_slots() -> None:
    ring = UtcRing(seconds=60.0)
    slot0 = np.arange(180_000, dtype=np.int16)
    slot1 = np.full(180_000, -7, dtype=np.int16)
    ring.write(slot0, 0.0)
    ring.write(slot1, 15.0)

    assert ring.read_slot(0) == slot0.tobytes()
    assert ring.read_slot(1) == slot1.tobytes()
    assert ring.read_slot(2) is None  # not yet complete
    assert ring.metrics.gaps == 0


def test_ring_gap_invalidates_only_the_missing_span() -> None:
    ring = UtcRing(seconds=90.0)
    slot0 = np.full(180_000, 1, dtype=np.int16)
    slot2 = np.full(180_000, 3, dtype=np.int16)
    ring.write(slot0, 0.0)
    ring.write(slot2, 30.0)

    assert ring.read_slot(0) == slot0.tobytes()
    assert ring.read_slot(1) is None  # the gap
    assert ring.read_slot(2) == slot2.tobytes()
    assert ring.metrics.gaps == 1


def test_ring_evicts_old_slots_and_counts_late_writes() -> None:
    ring = UtcRing(seconds=20.0)  # 240,000 samples < two slots
    slot0 = np.full(180_000, 1, dtype=np.int16)
    slot1 = np.full(180_000, 2, dtype=np.int16)
    slot2 = np.full(180_000, 3, dtype=np.int16)
    ring.write(slot0, 0.0)
    ring.write(slot1, 15.0)
    ring.write(slot2, 30.0)

    assert ring.read_slot(0) is None  # evicted
    assert ring.read_slot(2) == slot2.tobytes()

    ring.write(np.zeros(100, dtype=np.int16), 0.0)  # arrives after eviction
    assert ring.metrics.dropped_samples == 100


def test_ring_tolerates_overlapping_rewrite() -> None:
    ring = UtcRing(seconds=60.0)
    block = np.full(12_000, 5, dtype=np.int16)
    ring.write(block, 0.0)
    ring.write(block, 0.0)  # duplicate delivery
    ring.write(block, 1.0)
    assert ring.metrics.gaps == 0


class FakeStream:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.callback = kwargs["callback"]
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def close(self) -> None:
        self.closed = True


class FakeClock:
    def __init__(self) -> None:
        self.epoch = 0.0

    def __call__(self) -> float:
        return self.epoch


def test_capture_wires_stream_converter_and_ring() -> None:
    ring = UtcRing(seconds=60.0)
    clock = FakeClock()
    created: list[FakeStream] = []

    def factory(**kwargs: object) -> FakeStream:
        stream = FakeStream(**kwargs)
        created.append(stream)
        return stream

    capture = AudioCapture(
        ring, clock=clock, stream_factory=factory, blocksize=48_000
    )
    stream = created[0]
    assert stream.kwargs["samplerate"] == RX_SAMPLE_RATE
    assert stream.kwargs["channels"] == 1
    assert stream.kwargs["dtype"] == "float32"

    capture.start()
    assert stream.started

    block = sine(1_500.0, 48_000).reshape(-1, 1)
    # One 48 kHz block per test second; 16 blocks cover slot 0 (abs 0..180000).
    for index in range(16):
        clock.epoch = float(index)
        status = SimpleNamespace(input_overflow=index == 3)
        stream.callback(block, 48_000, None, status)

    assert capture.overruns == 1
    assert ring.metrics.gaps == 0
    slot_bytes = ring.read_slot(0)
    assert slot_bytes is not None
    pcm = np.frombuffer(slot_bytes, dtype="<i2")
    spectrum = np.abs(np.fft.rfft(pcm.astype(np.float64)))
    peak_hz = float(np.argmax(spectrum)) * DECODER_SAMPLE_RATE / pcm.size
    assert abs(peak_hz - 1_500.0) < 5.0

    capture.stop()
    assert stream.closed
    capture.stop()  # idempotent


def test_capture_tolerates_real_callback_jitter() -> None:
    """±3 ms per-block clock jitter must not carve gaps (acceptance finding)."""

    ring = UtcRing(seconds=60.0)
    clock = FakeClock()
    created: list[FakeStream] = []

    def factory(**kwargs: object) -> FakeStream:
        stream = FakeStream(**kwargs)
        created.append(stream)
        return stream

    capture = AudioCapture(
        ring, clock=clock, stream_factory=factory, blocksize=48_000
    )
    capture.start()
    block = sine(1_500.0, 48_000).reshape(-1, 1)
    jitter = [0.0, 0.003, -0.002, 0.001, -0.003]
    for index in range(16):
        clock.epoch = float(index + 1) + jitter[index % len(jitter)]
        created[0].callback(block, 48_000, None, None)

    assert ring.metrics.gaps == 0
    assert ring.read_slot(0) is not None


def test_capture_reanchors_after_a_real_stall() -> None:
    """A stall beyond the threshold is re-anchored and recorded as one gap."""

    ring = UtcRing(seconds=60.0)
    clock = FakeClock()
    created: list[FakeStream] = []

    def factory(**kwargs: object) -> FakeStream:
        stream = FakeStream(**kwargs)
        created.append(stream)
        return stream

    capture = AudioCapture(
        ring, clock=clock, stream_factory=factory, blocksize=48_000
    )
    capture.start()
    block = sine(1_500.0, 48_000).reshape(-1, 1)
    for index in range(4):
        clock.epoch = float(index + 1)
        created[0].callback(block, 48_000, None, None)
    # Stream stalls for a second, then resumes: re-anchor, ring sees a gap.
    for index in range(4, 8):
        clock.epoch = float(index + 2)
        created[0].callback(block, 48_000, None, None)

    assert ring.metrics.gaps == 1
    assert ring.read_slot(0) is None  # intersects the stalled span
