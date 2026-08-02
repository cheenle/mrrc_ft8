"""Real-radio acceptance runner for the FT-710 (SDD SC9, §12.2, §15).

Hardware-gated; NOT part of the hardware-free pytest run.  Drives the real
server components against a live rigctld and the rig's USB audio codec:

- ``preflight``: rigctld reachable, PTT off, audio devices open at 48 kHz,
  DSP Worker starts.
- ``monitor``: tune to the FT8 QRG, capture one exact UTC slot from the
  live audio, decode it through the supervised Worker and report latency.
- ``tx``: gated behind ``--tx``.  Safety-controller-keyed short tone,
  PTT on/off verification and a mid-TX priority STOP (§15).  This
  transmits RF — run only into a dummy load or with the operator ready.

Example:
    venv/bin/python acceptance/real_radio.py --audio-in 3 --audio-out 2
    venv/bin/python acceptance/real_radio.py --audio-in 3 --audio-out 2 \
        --tx --tx-freq 7074000
"""

from __future__ import annotations

import os

os.environ.setdefault("OMP_STACKSIZE", "10M")  # before NumPy/OpenMP loads

import argparse
import asyncio
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.core.supervisor import WorkerSupervisor
from server.engine.audio_rx import AudioCapture, UtcRing
from server.engine.audio_tx import TxPlayer
from server.engine.dsp_decode import SupervisorDecoder
from server.engine.latency import LatencyHistogram
from server.engine.orchestrator import FT8_PERIOD_SECONDS, slot_id_for
from server.engine.rig import RigClient
from server.engine.safety import SafetyController

RESULTS: list[tuple[str, bool, str]] = []


def report(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


async def preflight(rig: RigClient, args: argparse.Namespace) -> bool:
    ok = True
    freq = await rig.get_frequency()
    mode, width = await rig.get_mode()
    report("rigctld reachable", True, f"{freq} Hz {mode} {width} Hz")
    ptt = await rig.get_ptt()
    report("PTT off at start", not ptt, f"ptt={ptt}")
    ok = ok and not ptt

    import sounddevice as sd

    for role, index in (("input", args.audio_in), ("output", args.audio_out)):
        try:
            info = sd.query_devices(index)
            report(
                f"audio {role} device",
                True,
                f"#{index} {info['name']}",
            )
        except Exception as exc:  # noqa: BLE001 — acceptance reports any failure
            report(f"audio {role} device", False, str(exc))
            ok = False
    return ok


async def monitor(
    rig: RigClient,
    supervisor: WorkerSupervisor,
    args: argparse.Namespace,
) -> bool:
    """Tune to the FT8 QRG and decode one exact live UTC slot."""

    original = (await rig.get_frequency(), *await rig.get_mode())
    await rig.set_frequency(args.freq)
    await rig.set_mode(args.mode, 0)
    freq = await rig.get_frequency()
    report("tuned to FT8 QRG", freq == args.freq, f"{freq} Hz {args.mode}")

    ring = UtcRing(seconds=60.0)
    capture = AudioCapture(ring, device=args.audio_in)
    histogram = LatencyHistogram()
    decoder = SupervisorDecoder(supervisor, histogram=histogram)
    ok = True
    try:
        await asyncio.to_thread(capture.start)
        slot_id = slot_id_for(time.time()) + 1  # first fully captured slot
        wait = (slot_id + 1) * FT8_PERIOD_SECONDS - time.time() + 0.3
        print(f"  capturing slot {slot_id} (waiting {wait:.1f} s)…")
        await asyncio.sleep(wait)
        samples = ring.read_slot(slot_id)
        if samples is None:
            report("live slot capture", False, "ring gap/underrun")
            return False
        report("live slot capture", True, f"{len(samples)} bytes, rms check")
        pcm = np.frombuffer(samples, dtype="<i2")
        rms = float(np.sqrt(np.mean(pcm.astype(np.float64) ** 2)))
        report("audio not silent", rms > 10.0, f"rms={rms:.1f} (int16)")
        batch = await decoder.decode(slot_id, samples)
        report(
            "supervised live decode",
            True,
            f"{len(batch.results)} decode(s), "
            f"native {batch.elapsed_seconds:.2f} s",
        )
        for result in batch.results[:10]:
            print(f"    snr {result.snr:+3d} dt {result.dt:+.1f} "
                  f"{result.frequency:7.1f} Hz  {result.text}")
        latency = histogram.snapshot().get("p3/t1", {})
        if latency:
            print(f"  wall latency: {latency}")
    except Exception as exc:  # noqa: BLE001
        report("monitor phase", False, repr(exc))
        ok = False
    finally:
        await asyncio.to_thread(capture.stop)
        decoder.close()
        await rig.set_frequency(original[0])
        await rig.set_mode(original[1], original[2])
    return ok


async def tx_phase(rig: RigClient, args: argparse.Namespace) -> bool:
    """PTT-keyed tone and mid-TX priority STOP (§15); transmits RF."""

    await rig.set_frequency(args.tx_freq)
    await rig.set_mode(args.mode, 0)
    player = TxPlayer(device=args.audio_out)
    safety = SafetyController(rig, player)
    await safety.start()
    ptt = await rig.get_ptt()
    report("safety start is PTT-off", not ptt, f"ptt={ptt}")
    await safety.arm()

    rate = 48_000
    tone = 0.5 * np.sin(
        2 * np.pi * 1_500 * np.arange(int(rate * args.tx_seconds)) / rate
    ).astype(np.float32)
    started = time.monotonic()
    try:
        await safety.transmit(tone)
        elapsed = time.monotonic() - started
        ptt_after = await rig.get_ptt()
        report(
            "PTT-keyed tone transmission",
            not ptt_after,
            f"{args.tx_seconds:.1f} s tone, PTT released after {elapsed:.2f} s",
        )
    except Exception as exc:  # noqa: BLE001
        report("PTT-keyed tone transmission", False, repr(exc))
        await safety.stop_tx("acceptance-failure")
        return False

    # §15: mid-TX priority STOP cancels audio and drops PTT immediately.
    # (10 s stays under the one-FT8-waveform 606,720-sample buffer cap.)
    # Production transmissions are ≥7.5 s apart; give the USB codec a beat
    # before reopening its stream.
    await asyncio.sleep(2.0)
    long_tone = 0.5 * np.sin(
        2 * np.pi * 1_500 * np.arange(rate * 10) / rate
    ).astype(np.float32)
    task = asyncio.create_task(safety.transmit(long_tone))
    await asyncio.sleep(2.0)
    keyed = await rig.get_ptt()
    stop_started = time.monotonic()
    await safety.stop_tx("acceptance-stop-check")
    stop_latency = time.monotonic() - stop_started
    ptt_after_stop = await rig.get_ptt()
    transmit_error: str | None = None
    try:
        await task
    except Exception as exc:  # noqa: BLE001 — STOP during TX surfaces here
        transmit_error = repr(exc)
    if transmit_error is not None:
        print(f"  second transmit raised: {transmit_error}")
    report(
        "mid-TX priority STOP",
        keyed and not ptt_after_stop,
        f"keyed={keyed}, PTT released in {stop_latency:.2f} s",
    )
    health = safety.health
    print(f"  safety health: {health}")
    return keyed and not ptt_after_stop


async def run(args: argparse.Namespace) -> int:
    rig = RigClient(host=args.rigctld_host, port=args.rigctld_port)
    supervisor = WorkerSupervisor()
    await asyncio.to_thread(supervisor.start)
    report("DSP Worker starts", True)
    try:
        ok = await preflight(rig, args)
        if ok:
            ok = await monitor(rig, supervisor, args)
        if ok and args.tx:
            ok = await tx_phase(rig, args)
    finally:
        await asyncio.to_thread(supervisor.stop)
        await rig.close()
    failed = [name for name, passed, _ in RESULTS if not passed]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rigctld", default="127.0.0.1:4532")
    parser.add_argument("--audio-in", type=int, required=True)
    parser.add_argument("--audio-out", type=int, required=True)
    parser.add_argument("--freq", type=int, default=7_074_000, help="monitor QRG")
    parser.add_argument("--mode", default="USB")
    parser.add_argument("--tx", action="store_true", help="enable TX phase (RF!)")
    parser.add_argument("--tx-freq", type=int, default=7_074_000)
    parser.add_argument("--tx-seconds", type=float, default=2.0)
    args = parser.parse_args()
    host, _, port = args.rigctld.partition(":")
    args.rigctld_host, args.rigctld_port = host, int(port or 4532)
    if args.tx:
        reply = input(
            f"TX phase will key the radio on {args.tx_freq} Hz for "
            f"{args.tx_seconds} s plus a STOP test. Type TX to proceed: "
        )
        if reply.strip() != "TX":
            print("TX phase aborted by operator")
            args.tx = False
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
