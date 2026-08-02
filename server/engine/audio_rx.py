"""48 kHz receive path: one conversion, UTC ring and capture glue (AD-004).

``RxConverter`` performs the single allowed 48 kHz float32 → 12 kHz int16
mono conversion as a streaming FIR anti-alias filter plus 4:1 decimation, so
arbitrary block boundaries never lose or duplicate samples.  ``UtcRing``
indexes the converted stream by absolute UTC sample number and serves exact
180,000-sample slots, returning ``None`` on gaps, underruns and not-yet
complete slots.  ``AudioCapture`` is the thin sounddevice seam; the stream
factory is injectable so tests stay hardware-free.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

import logging

import numpy as np
import scipy.signal

_audio_log = logging.getLogger("mrrc-ft8.audio")

RX_SAMPLE_RATE = 48_000
DECODER_SAMPLE_RATE = 12_000
DECIMATION = RX_SAMPLE_RATE // DECODER_SAMPLE_RATE
SLOT_SAMPLES = 180_000  # one 15 s FT8 slot at 12 kHz
ANTI_ALIAS_CUTOFF_HZ = 5_400.0
FIR_TAPS = 97
DEFAULT_RING_SECONDS = 60.0
RESYNC_THRESHOLD_SECONDS = 0.25
"""Wall-clock drift beyond this re-anchors the sample-count epoch chain."""


class RxConverter:
    """Streaming 48 kHz → 12 kHz converter; the only RX conversion (AD-004).

    The FIR group delay is constant ((taps-1)/2 input samples ≈ 1 ms) and
    applies uniformly to all audio, so UTC slot alignment is unaffected.
    """

    def __init__(self, taps: int = FIR_TAPS, cutoff_hz: float = ANTI_ALIAS_CUTOFF_HZ) -> None:
        self._fir = scipy.signal.firwin(taps, cutoff_hz / (RX_SAMPLE_RATE / 2))
        self._zi = scipy.signal.lfilter_zi(self._fir, 1.0) * 0.0
        self._consumed = 0  # absolute filtered input samples so far

    def push(self, pcm: np.ndarray) -> np.ndarray:
        """Convert one 48 kHz float32 block; returns 12 kHz int16 samples."""

        block = np.asarray(pcm, dtype=np.float32)
        filtered, self._zi = scipy.signal.lfilter(self._fir, 1.0, block, zi=self._zi)
        first = (-self._consumed) % DECIMATION
        self._consumed += filtered.size
        decimated = filtered[first::DECIMATION]
        return np.clip(np.rint(decimated * 32_767.0), -32_768, 32_767).astype("<i2")


@dataclass(slots=True)
class RingMetrics:
    """Loss accounting for the receive ring."""

    dropped_samples: int = 0  # arrived after eviction (overrun)
    gaps: int = 0             # discontinuous writes (lost audio upstream)


class UtcRing:
    """UTC-indexed contiguous 12 kHz int16 ring serving exact slots.

    Absolute sample index ``floor(epoch * 12000)`` defines position; a slot is
    readable only while its whole range lies inside ``[base, high_water)`` and
    intersects no recorded gap range (AD-006 discipline applied to audio).
    """

    def __init__(
        self,
        seconds: float = DEFAULT_RING_SECONDS,
        *,
        rate: int = DECODER_SAMPLE_RATE,
        slot_samples: int = SLOT_SAMPLES,
    ) -> None:
        if seconds <= 0 or slot_samples <= 0:
            raise ValueError("ring capacity and slot size must be positive")
        self._rate = rate
        self._slot_samples = slot_samples
        self._capacity = int(seconds * rate)
        self._buffer = np.zeros(self._capacity, dtype="<i2")
        self._base: int | None = None      # oldest retained absolute index
        self._high_water = 0               # absolute contiguous end (exclusive)
        self._gap_ranges: list[tuple[int, int]] = []
        self.metrics = RingMetrics()

    def write(self, samples: np.ndarray, first_epoch: float) -> None:
        """Append converted samples whose first sample sits at first_epoch."""

        data = np.asarray(samples, dtype="<i2")
        if data.size == 0:
            return
        first = round(first_epoch * self._rate)
        # Nearest-sample quantization: a sample-count-anchored epoch chain
        # lands ±1 ulp either side of the integer (0.003 s → 35.999…/36.000…);
        # truncating would alias those into one-sample drops and gaps.
        if self._base is None:
            self._base = self._high_water = first

        end = first + data.size
        if end <= self._base:
            self.metrics.dropped_samples += data.size
            return
        if first > self._high_water:
            # Upstream lost audio between high_water and first.
            self.metrics.gaps += 1
            self._gap_ranges.append((self._high_water, first))
        start = max(first, self._high_water, self._base)
        if start > first:
            self.metrics.dropped_samples += start - first
        chunk = data[start - first :]
        new_high = start + chunk.size
        if new_high - self._base > self._capacity:
            evict_to = new_high - self._capacity
            self._base = evict_to
            self._gap_ranges = [
                (max(gap_start, evict_to), gap_end)
                for gap_start, gap_end in self._gap_ranges
                if gap_end > evict_to
            ]
        offset = start - self._base
        positions = (np.arange(offset, offset + chunk.size)) % self._capacity
        self._buffer[positions] = chunk
        self._high_water = new_high

    def read_slot(self, slot_id: int) -> bytes | None:
        """Return the exact slot bytes, or None on gap/underrun/incomplete."""

        if self._base is None:
            return None
        first = slot_id * self._slot_samples
        last = first + self._slot_samples
        if first < self._base or last > self._high_water:
            return None
        for gap_start, gap_end in self._gap_ranges:
            if first < gap_end and last > gap_start:
                return None
        offset = first - self._base
        positions = np.arange(offset, offset + self._slot_samples) % self._capacity
        return self._buffer[positions].tobytes()


class AudioCapture:
    """48 kHz mono input stream feeding the converter and UTC ring.

    ``stream_factory`` defaults to ``sounddevice.InputStream`` (imported
    lazily so hardware-free hosts can use the pure components); tests inject
    a fake.  ``clock`` supplies UTC epoch seconds for ring indexing.
    """

    def __init__(
        self,
        ring: UtcRing,
        converter: RxConverter | None = None,
        *,
        clock: Callable[[], float] = time.time,
        stream_factory: Callable[..., object] | None = None,
        device: int | str | None = None,
        blocksize: int = 4_096,
        tap: Callable[[np.ndarray, float], None] | None = None,
    ) -> None:
        if stream_factory is None:
            import sounddevice

            stream_factory = sounddevice.InputStream
        self._ring = ring
        self._converter = converter or RxConverter()
        self._clock = clock
        self._tap = tap
        self.overruns = 0
        self._next_epoch: float | None = None  # sample-count-anchored continuity
        self._stream = stream_factory(
            samplerate=RX_SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=blocksize,
            device=device,
            callback=self._on_block,
        )

    def _on_block(
        self, indata: np.ndarray, frames: int, time_info: object, status: object
    ) -> None:
        if status and getattr(status, "input_overflow", False):
            self.overruns += 1
        # Anchor epochs to the sample count, not per-block wall reads: real
        # callback jitter (±ms) would otherwise carve micro-gaps into the
        # ring and invalidate every slot (real-radio acceptance finding).
        # The wall clock only anchors the stream start and re-anchors after
        # a genuine stall (device hiccup), which the ring records as a gap.
        block_start = self._clock() - frames / RX_SAMPLE_RATE
        if (
            self._next_epoch is None
            or abs(block_start - self._next_epoch) > RESYNC_THRESHOLD_SECONDS
        ):
            self._next_epoch = block_start
        block_epoch = self._next_epoch
        self._next_epoch += frames / RX_SAMPLE_RATE
        converted = self._converter.push(indata[:, 0])
        self._ring.write(converted, block_epoch)
        if self._tap is not None:
            self._tap(converted, block_epoch)  # waterfall feed (§11.2)

    def start(self) -> None:
        """Start the input stream."""

        self._stream.start()  # type: ignore[attr-defined]

    def stop(self) -> None:
        """Stop and close the input stream; idempotent."""

        if self._stream is not None:
            self._stream.stop()  # type: ignore[attr-defined]
            self._stream.close()  # type: ignore[attr-defined]
            self._stream = None
            self._next_epoch = None  # re-anchor on the next start
