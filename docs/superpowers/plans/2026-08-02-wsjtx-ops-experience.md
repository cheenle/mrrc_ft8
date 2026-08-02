# WSJT-X 操作体验对齐 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Git 注意:** 本仓库当前无任何提交;每次 commit 步骤执行前必须得到用户明确确认(system 规则:未经要求不做 git mutation)。

**Goal:** 让 Web 驾驶舱具备完整 TX 通路(编码→发射→落库),并在此之上实现自动 CQ 循环与 Band Activity 式解码表(spec: `docs/superpowers/specs/2026-08-02-wsjtx-ops-experience-design.md`)。

**Architecture:** 新增 `dsp_encode.py`(Worker 编码通路,镜像 `dsp_decode.py`)、`tx_driver.py`(时隙 TX 驱动,挂 orchestrator `on_slot_start`)、`cq_loop.py`(循环控制器,纯观察 sequencer 状态迁移);`main.py` 接线 + 1 秒 watchdog 扩展;API 增 `cq.loop` 字段、`cq_loop_idle_timeout_s` 设置项、快照 `cq_loop` 子对象;前端 candidates/safety/api/state 四处小改。所有 PTT 仍只在 `safety.py` 边界。

**Tech Stack:** Python 3.12 asyncio / FastAPI / multiprocessing shared_memory / NumPy;前端 vanilla JS(无构建);pytest。

**通用约束(每个任务都适用):**
- 测试命令一律 `venv/bin/python -m pytest <path> -q`。
- 硬件无关:DSP/音频/电台一律在边界 fake(参照 `tests/engine/test_dsp_decode.py` 的 `FakeSupervisor`)。
- 提交信息格式:`feat: <一句话>` / `fix: <一句话>`。

---

## Task 1: SupervisorEncoder(生产编码通路)

**Files:**
- Create: `server/engine/dsp_encode.py`
- Test: `tests/engine/test_dsp_encode.py`

- [ ] **Step 1: 写失败测试**

```python
from __future__ import annotations

import asyncio
from multiprocessing.shared_memory import SharedMemory

import numpy as np
import pytest

from server.core.protocol import encode_frame
from server.engine.dsp_encode import SupervisorEncoder, TxEncodeError

TX_NBYTES = 606_720 * 4


class FakeSupervisor:
    """Captures the request frame and answers with a scripted response."""

    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.frames: list[dict[str, object]] = []
        self.timeouts: list[float] = []

    def request(self, frame: dict[str, object], timeout: float) -> dict[str, object]:
        encode_frame({**frame, "v": 1, "generation": 1, "request_id": 1})
        self.frames.append(frame)
        self.timeouts.append(timeout)
        return self.response


def encode_ok(message: str = "CQ M0XX IO91") -> dict[str, object]:
    return {
        "v": 1,
        "type": "encode_ok",
        "generation": 1,
        "request_id": 2,
        "slot_id": 0,
        "message": message,
        "sample_rate": 48_000,
        "sample_count": 606_720,
    }


def run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


def test_encode_frame_contract_and_waveform_copy() -> None:
    supervisor = FakeSupervisor(encode_ok())
    with SupervisorEncoder(supervisor) as encoder:  # type: ignore[arg-type]
        waveform = run(encoder.encode("CQ M0XX IO91", 1500.0, slot_id=42))
    frame = supervisor.frames[0]
    assert frame["type"] == "encode"
    assert frame["slot_id"] == 42
    assert frame["message"] == "CQ M0XX IO91"
    assert frame["frequency"] == 1500.0
    assert frame["sample_rate"] == 48_000
    assert frame["shm"]["dtype"] == "<f4"
    assert frame["shm"]["shape"] == [606_720]
    assert frame["shm"]["nbytes"] == TX_NBYTES
    assert isinstance(waveform, np.ndarray)
    assert waveform.dtype == np.float32 and waveform.shape == (606_720,)


def test_segment_reused_across_requests() -> None:
    supervisor = FakeSupervisor(encode_ok())
    with SupervisorEncoder(supervisor) as encoder:  # type: ignore[arg-type]
        run(encoder.encode("CQ M0XX IO91", 1500.0, slot_id=1))
        run(encoder.encode("M0XX K1ABC -12", 1500.0, slot_id=2))
    names = {f["shm"]["name"] for f in supervisor.frames}
    assert len(names) == 1


def test_error_frame_raises_tx_encode_error() -> None:
    response = {
        "v": 1, "type": "error", "generation": 1, "request_id": 2,
        "slot_id": 0, "code": "dsp_error", "detail": "encode failed",
    }
    supervisor = FakeSupervisor(response)
    with SupervisorEncoder(supervisor) as encoder:  # type: ignore[arg-type]
        with pytest.raises(TxEncodeError) as excinfo:
            run(encoder.encode("BAD", 1500.0, slot_id=1))
    assert excinfo.value.code == "dsp_error"


def test_close_is_idempotent() -> None:
    encoder = SupervisorEncoder(FakeSupervisor(encode_ok()))  # type: ignore[arg-type]
    run(encoder.encode("CQ M0XX IO91", 1500.0, slot_id=1))
    encoder.close()
    encoder.close()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/python -m pytest tests/engine/test_dsp_encode.py -q`
Expected: FAIL,`ModuleNotFoundError: No module named 'server.engine.dsp_encode'`

- [ ] **Step 3: 实现 `server/engine/dsp_encode.py`**

```python
"""Production TX encoder: message → shared memory → supervised Worker.

Mirror of ``dsp_decode.SupervisorDecoder`` for the Protocol v1 ``encode``
request (SDD §11.4): one reusable caller-owned 606,720-sample float32
segment carries the 48 kHz waveform back; the blocking supervisor round
trip runs in ``asyncio.to_thread`` so the engine loop never stalls.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from multiprocessing.shared_memory import SharedMemory

import numpy as np

from server.core.supervisor import WorkerSupervisor

TX_SAMPLE_RATE = 48_000
TX_SAMPLES = 606_720
TX_NBYTES = TX_SAMPLES * 4
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0


class TxEncodeError(Exception):
    """The Worker returned a sanitized application-level error frame."""

    __slots__ = ("code", "detail")

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class SupervisorEncoder:
    """Encode standard FT8 messages through the supervised Worker."""

    def __init__(
        self,
        supervisor: WorkerSupervisor,
        *,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if request_timeout <= 0:
            raise ValueError("request timeout must be positive")
        self._supervisor = supervisor
        self._request_timeout = request_timeout
        self._monotonic = monotonic
        self._shm: SharedMemory | None = None

    def _ensure_segment(self) -> SharedMemory:
        if self._shm is None:
            self._shm = SharedMemory(create=True, size=TX_NBYTES)
        return self._shm

    async def encode(self, message: str, frequency: float, *, slot_id: int = 0) -> np.ndarray:
        """Encode one message; returns a caller-owned float32 waveform copy."""

        shm = self._ensure_segment()
        frame = {
            "type": "encode",
            "slot_id": slot_id,
            "deadline_monotonic": self._monotonic() + self._request_timeout,
            "message": message,
            "frequency": frequency,
            "sample_rate": TX_SAMPLE_RATE,
            "shm": {
                "name": shm.name,
                "dtype": "<f4",
                "shape": [TX_SAMPLES],
                "nbytes": TX_NBYTES,
            },
        }
        response = await asyncio.to_thread(
            self._supervisor.request, frame, self._request_timeout
        )
        if response["type"] == "error":
            raise TxEncodeError(str(response["code"]), str(response["detail"]))
        return np.array(shm.buf[:TX_NBYTES], dtype=np.float32).copy()

    def close(self) -> None:
        """Close and unlink the shared TX segment; idempotent."""

        if self._shm is not None:
            shm, self._shm = self._shm, None
            shm.close()
            shm.unlink()

    def __enter__(self) -> SupervisorEncoder:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
```

注意:`np.array(shm.buf[:TX_NBYTES], dtype=np.float32)` 在 buf 切片上按 float32 重塑需用 `np.frombuffer(bytes(shm.buf[:TX_NBYTES]), dtype="<f4").astype(np.float32)`——若直接 view 报 misaligned/只读问题,用 frombuffer 版本。以实际运行为准。

- [ ] **Step 4: 跑测试确认通过**

Run: `venv/bin/python -m pytest tests/engine/test_dsp_encode.py -q`
Expected: 4 passed

- [ ] **Step 5: Commit(需用户确认)**

```bash
git add server/engine/dsp_encode.py tests/engine/test_dsp_encode.py
git commit -m "feat: add supervised Worker TX encoder"
```

---

## Task 2: TxDriver(时隙 TX 驱动)

**Files:**
- Create: `server/engine/tx_driver.py`
- Test: `tests/engine/test_tx_driver.py`

- [ ] **Step 1: 写失败测试**

```python
from __future__ import annotations

import asyncio

import numpy as np
import pytest

from server.engine.dsp_encode import TxEncodeError
from server.engine.safety import TxRefused
from server.engine.sequencer import Sequencer
from server.engine.tx_driver import TxDriver

WAVEFORM = np.zeros(606_720, dtype=np.float32)


class FakeEncoder:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, float, int]] = []
        self.error = error

    async def encode(self, message: str, frequency: float, *, slot_id: int) -> np.ndarray:
        self.calls.append((message, frequency, slot_id))
        if self.error is not None:
            raise self.error
        return WAVEFORM


class FakeSafety:
    def __init__(self, error: Exception | None = None) -> None:
        self.transmissions: list[np.ndarray] = []
        self.error = error

    async def transmit(self, samples: np.ndarray) -> None:
        if self.error is not None:
            raise self.error
        self.transmissions.append(samples)


def run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


def make_driver(encoder: FakeEncoder, safety: FakeSafety) -> tuple[Sequencer, TxDriver]:
    sequencer = Sequencer(my_call="M0XX", my_grid="IO91")
    driver = TxDriver(sequencer, encoder, safety)  # type: ignore[arg-type]
    return sequencer, driver


def test_cq_transmits_on_even_slots_only() -> None:
    sequencer, driver = make_driver(FakeEncoder(), FakeSafety())
    sequencer.start_cq()
    run(driver.on_slot_start(0))   # even: TX
    run(driver.on_slot_start(1))   # odd: RX, no TX
    run(driver.on_slot_start(2))   # even: TX
    assert [c[0] for c in driver.encoder.calls] == ["CQ M0XX IO91", "CQ M0XX IO91"]
    assert [c[2] for c in driver.encoder.calls] == [0, 2]
    assert len(driver.safety.transmissions) == 2


def test_idle_sequencer_transmits_nothing() -> None:
    _sequencer, driver = make_driver(FakeEncoder(), FakeSafety())
    run(driver.on_slot_start(0))
    assert driver.encoder.calls == []


def test_encode_failure_counts_and_does_not_raise() -> None:
    sequencer, driver = make_driver(FakeEncoder(TxEncodeError("dsp_error", "x")), FakeSafety())
    sequencer.start_cq()
    run(driver.on_slot_start(0))
    assert driver.counters["tx_failed"] == 1
    assert driver.safety.transmissions == []


def test_tx_refused_counts_and_does_not_raise() -> None:
    sequencer, driver = make_driver(FakeEncoder(), FakeSafety(TxRefused("not armed")))
    sequencer.start_cq()
    run(driver.on_slot_start(0))
    assert driver.counters["tx_failed"] == 1


def test_retry_exhaustion_stops_transmissions() -> None:
    """CQ repeats forever by design (UC-004); the budget bounds QSO messages."""

    sequencer, driver = make_driver(FakeEncoder(), FakeSafety())
    sequencer.max_retransmissions = 0  # budget: one send only
    sequencer.reply_to(parse_message("CQ K1ABC FN42"), snr_db=-12)
    run(driver.on_slot_start(0))  # Tx1 sent
    run(driver.on_slot_start(2))  # budget exhausted → RETRY_EXHAUSTED, no encode
    assert len(driver.encoder.calls) == 1
    assert sequencer.tx_enabled is False
```

注意:测试顶部需 `from server.engine.msgparse import parse_message`。

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/python -m pytest tests/engine/test_tx_driver.py -q`
Expected: FAIL,`ModuleNotFoundError`

- [ ] **Step 3: 实现 `server/engine/tx_driver.py`**

```python
"""Slot-boundary TX driver: sequencer message → encode → gated transmit.

The orchestrator announces every slot start; this driver transmits only on
its configured parity (FT8 TX/RX alternation), pulls at most one message
per eligible slot from the sequencer (driving the NFR-055 budget), encodes
it through the supervised Worker and hands the waveform to the safety
controller.  Every failure is counted and left to the §15.5 fault matrix;
the driver never retries and never touches PTT itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .dsp_encode import TxEncodeError
from .safety import TxRefused
from .sequencer import Sequencer

DEFAULT_TX_AUDIO_FREQUENCY = 1500.0


@dataclass
class TxDriver:
    """One-message-per-eligible-slot transmission pump."""

    sequencer: Sequencer
    encoder: Any       # SupervisorEncoder; duck-typed for tests
    safety: Any        # SafetyController; duck-typed for tests
    tx_audio_frequency: float = DEFAULT_TX_AUDIO_FREQUENCY
    tx_parity: int = 0  # transmit on even slot ids, receive on odd
    counters: dict[str, int] = field(
        default_factory=lambda: {"tx_attempts": 0, "tx_failed": 0}
    )

    async def on_slot_start(self, slot_id: int) -> None:
        """Handle one orchestrator slot-start announcement."""

        if slot_id % 2 != self.tx_parity:
            return
        message = self.sequencer.next_tx_message()
        if message is None:
            return
        self.counters["tx_attempts"] += 1
        try:
            waveform = await self.encoder.encode(
                message, self.tx_audio_frequency, slot_id=slot_id
            )
            await self.safety.transmit(waveform)
        except (TxEncodeError, TxRefused) as error:
            self.counters["tx_failed"] += 1
            self.on_tx_error(slot_id, error)

    def on_tx_error(self, slot_id: int, error: Exception) -> None:
        """Hook for composition-layer audit/logging; default is a no-op."""
```

- [ ] **Step 4: 跑测试确认通过**

Run: `venv/bin/python -m pytest tests/engine/test_tx_driver.py -q`
Expected: 5 passed

- [ ] **Step 5: Commit(需用户确认)**

```bash
git add server/engine/tx_driver.py tests/engine/test_tx_driver.py
git commit -m "feat: add slot-boundary TX driver"
```

---

## Task 3: QSO 落库助手

**Files:**
- Create: `server/engine/qso_log.py`
- Test: `tests/engine/test_qso_log.py`

- [ ] **Step 1: 写失败测试**

```python
from __future__ import annotations

from server.engine.msgparse import parse_message
from server.engine.qso_log import pop_and_record_qso
from server.engine.repository import Repository
from server.engine.sequencer import Sequencer


def drive_full_qso(sequencer: Sequencer) -> None:
    """CQ side: answer → R+report → RR73 (completes and logs)."""

    sequencer.start_cq()
    sequencer.on_message(parse_message("M0XX K1ABC FN42"), snr_db=-12)
    sequencer.on_message(parse_message("M0XX K1ABC R-10"), snr_db=-10)
    sequencer.on_message(parse_message("M0XX K1ABC RR73"), snr_db=-9)
    sequencer.next_tx_message()  # courtesy 73
    sequencer.next_tx_message()  # partner silent → DONE


def test_completed_qso_is_recorded_once() -> None:
    repository = Repository(":memory:")
    sequencer = Sequencer(my_call="M0XX", my_grid="IO91")
    drive_full_qso(sequencer)
    assert pop_and_record_qso(sequencer, repository) == 1
    assert pop_and_record_qso(sequencer, repository) == 0  # popped exactly once
    qsos = repository.list_qsos()
    assert len(qsos) == 1 and qsos[0].dx_call == "K1ABC"


def test_idle_sequencer_records_nothing() -> None:
    repository = Repository(":memory:")
    sequencer = Sequencer(my_call="M0XX", my_grid="IO91")
    assert pop_and_record_qso(sequencer, repository) == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/python -m pytest tests/engine/test_qso_log.py -q`
Expected: FAIL,`ModuleNotFoundError`

- [ ] **Step 3: 实现 `server/engine/qso_log.py`**

```python
"""Sequencer log-record → canonical QSO store glue (§7.5, UC-005).

Polled on the composition watchdog: the sequencer holds a completed
``QSORecord`` exactly once; this helper moves it into the repository,
where the 30-second void window and ADIF export already apply.
"""

from __future__ import annotations

from .repository import Repository
from .sequencer import Sequencer


def pop_and_record_qso(sequencer: Sequencer, repository: Repository) -> int:
    """Record the pending completed QSO, if any; returns rows inserted."""

    record = sequencer.pop_log_record()
    if record is None:
        return 0
    repository.record_qso(record)
    return 1
```

- [ ] **Step 4: 跑测试确认通过**

Run: `venv/bin/python -m pytest tests/engine/test_qso_log.py -q`
Expected: 2 passed

- [ ] **Step 5: Commit(需用户确认)**

```bash
git add server/engine/qso_log.py tests/engine/test_qso_log.py
git commit -m "feat: wire sequencer log records into the QSO store"
```

---

## Task 4: main.py 接线 TX 驱动与落库

**Files:**
- Modify: `server/main.py`(create_server 的 start_dsp 块与 lifespan watchdog)
- Modify: `server/web/api.py:84`(AppState 增 `tx_driver` 字段)
- Test: `tests/web/test_main.py`

- [ ] **Step 1: 写失败测试(组合层,追加到 tests/web/test_main.py)**

```python
def test_composition_wires_tx_driver() -> None:
    app = create_server(make_config(), rig=FakeRig(), start_dsp=False, start_audio=False)
    with TestClient(app):
        state = app.state.app_state
        assert state.tx_driver is not None
        assert state.tx_driver.counters == {"tx_attempts": 0, "tx_failed": 0}
        assert state.tx_driver.sequencer is state.sequencer


def test_composition_records_completed_qso() -> None:
    app = create_server(make_config(), rig=FakeRig(), start_dsp=False, start_audio=False)
    with TestClient(app):
        state = app.state.app_state
        sequencer = state.sequencer
        sequencer.start_cq()
        sequencer.on_message(parse_message("M0XX K1ABC FN42"), snr_db=-12)
        sequencer.on_message(parse_message("M0XX K1ABC R-10"), snr_db=-10)
        sequencer.on_message(parse_message("M0XX K1ABC RR73"), snr_db=-9)
        sequencer.next_tx_message()
        sequencer.next_tx_message()  # DONE
        time.sleep(1.4)  # watchdog poll (1 s) records the QSO
        qsos = state.repository.list_qsos()
        assert [q.dx_call for q in qsos] == ["K1ABC"]
```

(`parse_message` 需 import;TestClient 内 lifespan 已在跑,直接 sleep 即可。)

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/python -m pytest tests/web/test_main.py -q`
Expected: FAIL(`state.tx_driver` 不存在 / QSO 未落库)

- [ ] **Step 3: 实现接线**

`server/web/api.py` AppState 增加字段(放在 `latency` 旁,保持 `Any` 风格):

```python
    tx_driver: Any = None
    cq_loop: Any = None  # Task 7 使用,本次先占位
```

`server/main.py` create_server:
1. 顶部 import 区加:`from .engine.qso_log import pop_and_record_qso`(局部 import 风格跟随现有代码,放在 start_dsp 块内)。
2. `start_dsp` 块内,orchestrator 创建时注入 slot-start 回调(`Orchestrator.__init__` 已有 `on_slot_start` 参数,见 orchestrator.py:119;callback 在 loop 线程内同步调用,直接 `asyncio.create_task`):

```python
        from .engine.dsp_encode import SupervisorEncoder
        from .engine.tx_driver import TxDriver

        encoder = SupervisorEncoder(supervisor)
        tx_driver = TxDriver(sequencer, encoder, safety)
        state.tx_driver = tx_driver
        orchestrator = Orchestrator(
            supervisor_decoder,
            slot_ring.read_slot,
            sequencer,
            on_decode=on_decode,
            on_slot_start=lambda slot_id: asyncio.create_task(
                tx_driver.on_slot_start(slot_id)
            ),
        )
```

`start_dsp=False` 分支:`state.tx_driver = TxDriver(sequencer, NoneEncoder(), safety)`,其中在 main.py 定义:

```python
class _NullEncoder:
    """TX driver placeholder when DSP is disabled; every encode fails fast."""

    async def encode(self, message: str, frequency: float, *, slot_id: int) -> Any:
        from .engine.dsp_encode import TxEncodeError

        raise TxEncodeError("dsp_unavailable", "TX encoder requires the DSP worker")
```

3. lifespan 的 `lease_watchdog` 改为同时轮询落库与(Task 7 的)循环 tick:

```python
        async def lease_watchdog() -> None:
            while True:
                await asyncio.sleep(LEASE_POLL_S)
                state.lease.check_expiry()
                await asyncio.to_thread(pop_and_record_qso, sequencer, repository)
                if state.cq_loop is not None:
                    state.cq_loop.tick()
```

4. shutdown 顺序:在 `supervisor_decoder.close` 前加 `if state.tx_driver is not None and hasattr(state.tx_driver.encoder, "close"): await asyncio.to_thread(state.tx_driver.encoder.close)`——实际把 encoder 引用存入 `encoder` 局部变量,复用现有 `supervisor` 关闭段:

```python
            if encoder is not None:
                await asyncio.to_thread(encoder.close)
```

(`encoder` 与 `supervisor_decoder` 同生命周期,在 `if start_dsp:` 顶部声明 `encoder = None`。)

- [ ] **Step 4: 跑测试确认通过**

Run: `venv/bin/python -m pytest tests/web/test_main.py tests/engine/ -q`
Expected: 全绿

- [ ] **Step 5: Commit(需用户确认)**

```bash
git add server/main.py server/web/api.py tests/web/test_main.py
git commit -m "feat: wire TX driver and QSO logging into the composition root"
```

---

## Task 5: CqLoopController(循环控制器)

**Files:**
- Create: `server/engine/cq_loop.py`
- Test: `tests/engine/test_cq_loop.py`

- [ ] **Step 1: 写失败测试**

```python
from __future__ import annotations

import pytest

from server.engine.cq_loop import CqLoopController, LoopStopReason
from server.engine.msgparse import parse_message
from server.engine.sequencer import DisarmReason, QSOState, Sequencer


class FakeArm:
    def __init__(self, refuse: bool = False) -> None:
        self.calls = 0
        self.refuse = refuse

    async def __call__(self) -> None:
        self.calls += 1
        if self.refuse:
            from server.engine.safety import TxRefused

            raise TxRefused("faulted")


def make_controller(
    *, lease: bool = True, timeout: int = 600, arm: FakeArm | None = None
) -> tuple[Sequencer, CqLoopController, list[float], list[tuple[str, str]]]:
    sequencer = Sequencer(my_call="M0XX", my_grid="IO91")
    now = [1000.0]
    audits: list[tuple[str, str]] = []
    controller = CqLoopController(
        sequencer,
        arm=arm or FakeArm(),
        lease_alive=lambda: lease,
        clock=lambda: now[0],
        idle_timeout=lambda: timeout,
        on_audit=lambda op, detail: audits.append((op, detail)),
    )
    return sequencer, controller, now, audits


def start(controller: CqLoopController) -> None:
    import asyncio

    asyncio.run(controller.start())


def test_done_rearms_cq_and_resets_idle_timer() -> None:
    sequencer, controller, now, _audits = make_controller()
    start(controller)
    assert sequencer.state == QSOState.CALLING
    sequencer.on_message(parse_message("M0XX K1ABC FN42"), snr_db=-12)
    sequencer.on_message(parse_message("M0XX K1ABC R-10"), snr_db=-10)
    sequencer.on_message(parse_message("M0XX K1ABC RR73"), snr_db=-9)
    sequencer.next_tx_message()
    sequencer.next_tx_message()  # DONE
    now[0] += 590  # close to the 600 s timeout
    controller.tick()
    assert sequencer.state == QSOState.CALLING  # re-armed
    now[0] += 590
    controller.tick()
    assert controller.active  # timer reset on DONE: still running


def test_retry_exhausted_rearms_without_resetting_timer() -> None:
    sequencer, controller, now, _audits = make_controller()
    start(controller)
    sequencer.on_message(parse_message("M0XX K1ABC FN42"), snr_db=-12)
    sequencer.max_retransmissions = 0
    sequencer.next_tx_message()  # initial report send
    sequencer.next_tx_message()  # budget exhausted
    assert sequencer.disarm_reason == DisarmReason.RETRY_EXHAUSTED
    controller.tick()
    assert sequencer.state == QSOState.CALLING and controller.active
    now[0] += 601
    controller.tick()
    assert not controller.active  # no DONE → timeout still fires


def test_partner_lost_rearms() -> None:
    sequencer, controller, _now, _audits = make_controller()
    start(controller)
    sequencer.on_message(parse_message("M0XX K1ABC FN42"), snr_db=-12)
    sequencer.on_message(parse_message("W1AW K1ABC -08"), snr_db=-8)
    assert sequencer.disarm_reason == DisarmReason.PARTNER_LOST
    controller.tick()
    assert sequencer.state == QSOState.CALLING and controller.active


def test_manual_disarm_stops_loop() -> None:
    sequencer, controller, _now, audits = make_controller()
    start(controller)
    sequencer.stop(DisarmReason.MANUAL)  # TX off
    controller.tick()
    assert not controller.active
    assert ("cq_loop_stop", "manual") in audits


def test_fault_disarm_stops_loop() -> None:
    sequencer, controller, _now, audits = make_controller()
    start(controller)
    sequencer.stop(DisarmReason.FAULT)
    controller.tick()
    assert not controller.active
    assert ("cq_loop_stop", "fault") in audits


def test_lease_loss_stops_loop() -> None:
    sequencer, controller, _now, audits = make_controller(lease=True)
    start(controller)
    controller.lease_alive = lambda: False  # lease dropped
    controller.tick()
    assert not controller.active
    assert ("cq_loop_stop", "lease_lost") in audits


def test_idle_timeout_stops_and_disarms() -> None:
    sequencer, controller, now, audits = make_controller(timeout=60)
    start(controller)
    now[0] += 61
    controller.tick()
    assert not controller.active
    assert sequencer.tx_enabled is False
    assert ("cq_loop_stop", "timeout") in audits


def test_start_is_idempotent_and_audited() -> None:
    _sequencer, controller, _now, audits = make_controller()
    start(controller)
    start(controller)
    assert audits.count(("cq_loop_start", "600")) == 1


def test_arm_refusal_fails_start() -> None:
    _sequencer, controller, _now, audits = make_controller(arm=FakeArm(refuse=True))
    start(controller)
    assert not controller.active
    assert ("cq_loop_stop", "arm_refused") in audits


def test_status_shape() -> None:
    _sequencer, controller, now, _audits = make_controller(timeout=600)
    start(controller)
    now[0] += 100
    status = controller.status()
    assert status == {"active": True, "idle_remaining_s": 500}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/python -m pytest tests/engine/test_cq_loop.py -q`
Expected: FAIL,`ModuleNotFoundError`

- [ ] **Step 3: 实现 `server/engine/cq_loop.py`**

```python
"""Automatic CQ loop controller (spec §1, SDD chapter 15 invariants).

Wraps the sequencer without modifying its state machine: completed or
failed QSOs re-arm CQ calling; manual/fault disarms, lease loss and the
idle timeout stop the loop.  The controller polls on the composition
watchdog and never touches PTT, audio or the network itself.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum

from .sequencer import DisarmReason, QSOState, Sequencer

DEFAULT_IDLE_TIMEOUT_S = 600
MIN_IDLE_TIMEOUT_S = 60
MAX_IDLE_TIMEOUT_S = 3_600

_REARM_REASONS = frozenset(
    {DisarmReason.RETRY_EXHAUSTED, DisarmReason.PARTNER_LOST}
)
_STOP_REASONS = frozenset({DisarmReason.MANUAL, DisarmReason.FAULT})


class LoopStopReason(StrEnum):
    TIMEOUT = "timeout"
    MANUAL = "manual"
    FAULT = "fault"
    LEASE_LOST = "lease_lost"
    ARM_REFUSED = "arm_refused"


@dataclass
class CqLoopController:
    """Observe sequencer transitions; re-CQ or stop, per the spec table."""

    sequencer: Sequencer
    arm: Callable[[], Awaitable[None]]
    lease_alive: Callable[[], bool]
    clock: Callable[[], float]
    idle_timeout: Callable[[], int]
    on_audit: Callable[[str, str], None]
    active: bool = False
    _last_progress: float = field(default=0.0)
    _observed: tuple[QSOState, DisarmReason | None] = field(
        default=(QSOState.IDLE, None)
    )

    async def start(self) -> None:
        """Arm via the normal safety path and begin CQ calling; idempotent."""

        if self.active:
            return
        try:
            await self.arm()
        except Exception:
            self.on_audit("cq_loop_stop", LoopStopReason.ARM_REFUSED.value)
            return
        self.sequencer.start_cq()
        self.active = True
        self._last_progress = self.clock()
        self._observed = (self.sequencer.state, self.sequencer.disarm_reason)
        self.on_audit("cq_loop_start", str(self.idle_timeout()))

    def stop(self, reason: LoopStopReason) -> None:
        """Terminate the loop (TX state itself is owned by safety/sequencer)."""

        if not self.active:
            return
        self.active = False
        self.on_audit("cq_loop_stop", reason.value)

    def tick(self) -> None:
        """One watchdog poll: lease gate, transition table, idle timeout."""

        if not self.active:
            return
        if not self.lease_alive():
            self.stop(LoopStopReason.LEASE_LOST)
            return
        observed = (self.sequencer.state, self.sequencer.disarm_reason)
        state, reason = observed
        if observed != self._observed:
            self._observed = observed
            if state is QSOState.DONE:
                self._last_progress = self.clock()
                self.sequencer.start_cq()
            elif reason in _REARM_REASONS:
                self.sequencer.start_cq()  # failed QSO: re-CQ, no timer reset
            elif reason in _STOP_REASONS:
                self.stop(
                    LoopStopReason.FAULT
                    if reason is DisarmReason.FAULT
                    else LoopStopReason.MANUAL
                )
                return
            self._observed = (self.sequencer.state, self.sequencer.disarm_reason)
        if self.clock() - self._last_progress > self.idle_timeout():
            self.sequencer.stop(DisarmReason.MANUAL)
            self.stop(LoopStopReason.TIMEOUT)

    def status(self) -> dict[str, object]:
        """Snapshot view for the state broadcast."""

        remaining = 0
        if self.active:
            remaining = max(
                0,
                round(self.idle_timeout() - (self.clock() - self._last_progress)),
            )
        return {"active": self.active, "idle_remaining_s": remaining}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `venv/bin/python -m pytest tests/engine/test_cq_loop.py -q`
Expected: 10 passed

- [ ] **Step 5: Commit(需用户确认)**

```bash
git add server/engine/cq_loop.py tests/engine/test_cq_loop.py
git commit -m "feat: add automatic CQ loop controller"
```

---

## Task 6: API — cq loop 字段、设置项、快照

**Files:**
- Modify: `server/web/api.py`(SETTING_SCHEMA、operation_cq、_snapshot)
- Test: `tests/web/test_api.py`

- [ ] **Step 1: 写失败测试(追加到 tests/web/test_api.py,沿用该文件现有登录/租约 fixture 风格)**

```python
def test_cq_loop_requires_lease(client: TestClient) -> None:
    login(client)
    response = client.post("/api/v1/operation/cq", json={"loop": True})
    assert response.status_code == 409  # observer without lease


def test_cq_loop_start_and_snapshot(client: TestClient) -> None:
    login(client)
    acquire_lease(client)
    response = client.post("/api/v1/operation/cq", json={"loop": True})
    assert response.status_code == 200
    snapshot = client.get("/api/v1/state").json()
    assert snapshot["sequencer"]["cq_loop"]["active"] is True
    assert snapshot["sequencer"]["cq_loop"]["idle_remaining_s"] > 0


def test_cq_without_loop_keeps_legacy_behavior(client: TestClient) -> None:
    login(client)
    acquire_lease(client)
    response = client.post(
        "/api/v1/operation/cq", headers={"idempotency-key": "k1"}
    )
    assert response.status_code == 200
    snapshot = client.get("/api/v1/state").json()
    assert snapshot["sequencer"]["cq_loop"]["active"] is False


def test_cq_loop_timeout_setting_bounds(client: TestClient) -> None:
    login(client)
    assert client.put(
        "/api/v1/settings", json={"cq_loop_idle_timeout_s": 59}
    ).status_code == 422
    assert client.put(
        "/api/v1/settings", json={"cq_loop_idle_timeout_s": 3601}
    ).status_code == 422
    assert client.put(
        "/api/v1/settings", json={"cq_loop_idle_timeout_s": 300}
    ).status_code == 200
```

(`login`/`acquire_lease` 用该测试文件现有的 helper 名称,若不同以其为准;cq_loop 在组合根由 Task 7 接线,本任务测试前需 AppState.cq_loop 已由 fake 填充或等待 Task 7——顺序上本测试在 Task 7 完成后才能全绿,实施时把 Step 4 放到 Task 7 之后跑。)

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/python -m pytest tests/web/test_api.py -q -k cq_loop`
Expected: FAIL(422/快照无 cq_loop)

- [ ] **Step 3: 实现**

`SETTING_SCHEMA` 增加一行:

```python
    "cq_loop_idle_timeout_s": lambda v: isinstance(v, int) and not isinstance(v, bool) and 60 <= v <= 3600,
```

`operation_cq` 改为(关键 diff,完整替换现函数):

```python
    @router.post("/operation/cq")
    async def operation_cq(request: Request, session: Session = Depends(require_lease)) -> JSONResponse:
        await validate_mutation(request)
        if (hit := replay(request)) is not None:
            return hit
        content_length = request.headers.get("content-length")
        body = await request.json() if content_length and content_length != "0" else {}
        if not isinstance(body, dict):
            return _reject(422, "invalid_request")
        loop = bool(body.get("loop", False))
        if loop:
            if state.cq_loop is None:
                return _reject(503, "cq_loop_unavailable")
            await state.cq_loop.start()  # arms via safety; refusal is audited
            if not state.cq_loop.active:
                return _reject(409, REASON_INTERLOCK_OPEN)
        else:
            try:
                await state.safety.arm()
            except TxRefused as exc:
                return _reject(409, REASON_INTERLOCK_OPEN, detail=str(exc))
            state.sequencer.start_cq()
        await _audit(state, session, "cq", "", "loop" if loop else "")
        return await mutate(request, request.headers.get("idempotency-key"), 200, {"sequencer": state.sequencer.state.value})
```

`_snapshot` 的 `sequencer` 子对象增加:

```python
        "sequencer": {
            "state": sequencer.state.value,
            "tx_enabled": sequencer.tx_enabled,
            "dx_call": sequencer.dx_call,
            "cq_loop": (
                state.cq_loop.status()
                if state.cq_loop is not None
                else {"active": False, "idle_remaining_s": 0}
            ),
        },
```

- [ ] **Step 4:(Task 7 后)跑测试确认通过**

Run: `venv/bin/python -m pytest tests/web/test_api.py -q`
Expected: 全绿

- [ ] **Step 5: Commit(需用户确认,与 Task 7 可合并提交)**

```bash
git add server/web/api.py tests/web/test_api.py
git commit -m "feat: add CQ loop API, setting and snapshot field"
```

---

## Task 7: main.py 接线 CqLoopController

**Files:**
- Modify: `server/main.py`(create_server)
- Test: `tests/web/test_main.py`

- [ ] **Step 1: 写失败测试(追加)**

```python
def test_composition_wires_cq_loop() -> None:
    app = create_server(make_config(), rig=FakeRig(), start_dsp=False, start_audio=False)
    with TestClient(app):
        state = app.state.app_state
        assert state.cq_loop is not None
        assert state.cq_loop.status() == {"active": False, "idle_remaining_s": 0}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/python -m pytest tests/web/test_main.py -q -k cq_loop`
Expected: FAIL

- [ ] **Step 3: 实现**

create_server 中,sequencer 创建之后、`state` 构造之后(AppState 已在 Task 4 占好 `cq_loop` 字段):

```python
    from .engine.cq_loop import (
        DEFAULT_IDLE_TIMEOUT_S,
        CqLoopController,
    )

    def cq_loop_idle_timeout() -> int:
        value = repository.get_setting("cq_loop_idle_timeout_s")
        return int(value) if isinstance(value, int) else DEFAULT_IDLE_TIMEOUT_S

    def cq_loop_audit(operation: str, detail: str) -> None:
        schedule(
            asyncio.to_thread(
                repository.record_audit,
                actor="system",
                operation=operation,
                target="",
                detail=detail,
            )
        )

    state.cq_loop = CqLoopController(
        sequencer,
        arm=safety.arm,
        lease_alive=lambda: state.lease.current() is not None,
        clock=time.monotonic,
        idle_timeout=cq_loop_idle_timeout,
        on_audit=cq_loop_audit,
    )
```

(放在 `state.lease = LeaseService(...)` 赋值**之后**,因为 `lease_alive` 闭包引用 `state.lease`;`schedule` 闭包复用现有 dead-man 接线的那个。watchdog 的 `state.cq_loop.tick()` 已在 Task 4 加好。)

- [ ] **Step 4: 跑测试确认通过(含 Task 6 的 API 测试)**

Run: `venv/bin/python -m pytest tests/web/ tests/engine/ -q`
Expected: 全绿

- [ ] **Step 5: Commit(需用户确认)**

```bash
git add server/main.py tests/web/test_main.py
git commit -m "feat: wire CQ loop controller into the composition root"
```

---

## Task 8: on_decode 批次补 dt/freq/to_me

**Files:**
- Modify: `server/main.py:211`(on_decode 的 messages 映射)
- Test: `tests/web/test_main.py`

- [ ] **Step 1: 写失败测试(追加)**

```python
class FakeSlotMessage:
    class result:
        text = "M0XX K1ABC FN42"
        snr = -12
        dt = 0.12
        frequency = 1234.5

    class parsed:
        from_call = "K1ABC"
        grid = "FN42"
        is_cq = False
        to_call = "M0XX"


def test_decode_message_view_carries_band_activity_fields() -> None:
    from server.main import decode_message_view

    item = decode_message_view(FakeSlotMessage(), "M0XX")
    assert item["dt"] == 0.12
    assert item["freq"] == 1234.5
    assert item["to_me"] is True
    assert item["call"] == "K1ABC"
    assert item["is_cq"] is False
```

(`to_me` 由 `addressed_to(parsed, my_call)` 判定;`addressed_to` 只读 `is_cq`/`to_call` 属性,FakeSlotMessage 已满足。)

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/python -m pytest tests/web/test_main.py -q -k dt_freq_to_me`
Expected: FAIL(`decode_message_view` 不存在)

- [ ] **Step 3: 实现**

`server/main.py`:把 on_decode 的 message 映射提取为模块级函数并补字段:

```python
def decode_message_view(message: Any, my_call: str = "") -> dict[str, Any]:
    """One decode message → wire payload (Band Activity columns, §10.2)."""

    from .engine.msgparse import addressed_to

    parsed = message.parsed
    return {
        "text": message.result.text,
        "snr": message.result.snr,
        "dt": message.result.dt,
        "freq": message.result.frequency,
        "call": parsed.from_call,
        "grid": parsed.grid,
        "is_cq": parsed.is_cq,
        "to_me": addressed_to(parsed, my_call),
    }
```

`on_decode` 内 messages 列表改为 `[decode_message_view(m, config.my_call) for m in slot_decode.messages]`。

- [ ] **Step 4: 跑测试确认通过**

Run: `venv/bin/python -m pytest tests/web/test_main.py -q`
Expected: 全绿

- [ ] **Step 5: Commit(需用户确认)**

```bash
git add server/main.py tests/web/test_main.py
git commit -m "feat: carry dt/freq/to_me in decode batches"
```

---

## Task 9: 前端 Band Activity 表 + CQ LOOP 显示

**Files:**
- Modify: `server/web/static/js/api.js`(cq 带 loop)
- Modify: `server/web/static/js/state.js`(candidates 带 late)
- Modify: `server/web/static/js/candidates.js`(多列渲染 + 双击应答)
- Modify: `server/web/static/js/safety.js`(CQ LOOP 倒计时 + 按钮逻辑)
- Modify: `server/web/static/css/app.css`(行样式)
- Modify: `server/web/static/sw.js`(缓存 v7)
- Test: `tests/web/test_static.py`

- [ ] **Step 1: 写失败静态契约测试(追加到 tests/web/test_static.py)**

```python
def test_candidates_render_band_activity_columns() -> None:
    js = (STATIC / "js" / "candidates.js").read_text()
    for field in ("snr", "dt", "freq", "text", "slot_id"):
        assert field in js
    assert "dblclick" in js  # double-click replies (same api.reply path)


def test_api_cq_carries_loop_flag() -> None:
    js = (STATIC / "js" / "api.js").read_text()
    assert re.search(r'cq:\s*\(loop', js), "api.cq must accept a loop flag"


def test_safety_bar_shows_cq_loop_countdown() -> None:
    js = (STATIC / "js" / "safety.js").read_text()
    assert "cq_loop" in js and "CQ LOOP" in js
```

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/python -m pytest tests/web/test_static.py -q`
Expected: 3 failed

- [ ] **Step 3: 实现**

`api.js` 替换 cq 一行:

```javascript
  cq: (loop = false) =>
    request("/operation/cq", { method: "POST", idempotencyKey: key(), body: { loop } }),
```

`state.js` 的 additions 映射补 `late`:

```javascript
    .map((m) => ({ ...m, slot_id: batch.slot_id, late: batch.late }));
```

`candidates.js` 完整替换:

```javascript
// Band Activity list: compact WSJT-X-style decode rows (§10.2, spec §2).

import { api } from "./api.js";
import { getState, patch, subscribe } from "./state.js";

function slotUtc(slotId) {
  return new Date(slotId * 15_000).toISOString().slice(11, 19);
}

function rowText(c) {
  const snr = `${c.snr > 0 ? "+" : ""}${c.snr}`.padStart(4);
  const dt = `${c.dt >= 0 ? "+" : ""}${Number(c.dt).toFixed(1)}`.padStart(5);
  const freq = String(Math.round(c.freq)).padStart(5);
  return `${slotUtc(c.slot_id)} ${snr} ${dt} ${freq} ${c.text}`;
}

export function createCandidates(listElement) {
  function render() {
    const { candidates, selected } = getState();
    listElement.replaceChildren(
      ...candidates.map((candidate) => {
        const item = document.createElement("li");
        item.className = "candidate";
        if (candidate.is_cq) item.classList.add("cq");
        if (candidate.to_me) item.classList.add("to-me");
        if (candidate.late) item.classList.add("late");
        if (selected && selected.call === candidate.call) item.classList.add("selected");
        item.textContent = rowText(candidate);
        item.addEventListener("click", () => select(candidate));
        item.addEventListener("dblclick", () => reply(candidate));
        return item;
      }),
    );
  }

  async function select(candidate) {
    // Selecting never arms or transmits; it only enables the Reply button.
    const result = await api.select(candidate);
    if (result.ok) {
      patch({ selected: { call: candidate.call, grid: candidate.grid || "" } });
    }
  }

  async function reply(candidate) {
    // Double-click = select + Reply through the exact same gated paths.
    const chosen = await api.select(candidate);
    if (chosen.ok) {
      patch({ selected: { call: candidate.call, grid: candidate.grid || "" } });
      await api.reply();
    }
  }

  subscribe(render);
  render();
}
```

`safety.js` 修改两处:
1. `render()` 中 `sequencerState.textContent = sequencer.state;` 改为:

```javascript
    const loop = sequencer.cq_loop || { active: false, idle_remaining_s: 0 };
    if (loop.active) {
      const mm = String(Math.floor(loop.idle_remaining_s / 60)).padStart(2, "0");
      const ss = String(loop.idle_remaining_s % 60).padStart(2, "0");
      sequencerState.textContent = `CQ LOOP ${mm}:${ss}`;
    } else {
      sequencerState.textContent = sequencer.state;
    }
```

2. `buttons.cq.addEventListener("click", () => api.cq());` 改为 `() => api.cq(true)`(CQ 按钮 = 启动循环;停止仍走 TX off / STOP)。

`app.css` 追加:

```css
/* ---- Band Activity rows (spec §2) ---- */
#candidate-list .candidate {
  font: 12px/1.5 ui-monospace, monospace;
  white-space: pre; padding: 6px 10px;
}
.candidate.cq { font-weight: 700; }
.candidate.to-me { background: #3d2430; }
.candidate.late { opacity: 0.55; }
```

(原 `.candidate` 的 padding/flex 规则被更具体选择器覆盖即可,不删旧规则;确认无冲突后如有重复可合并。)

`sw.js`:`mrrc-ft8-shell-v6` → `mrrc-ft8-shell-v7`。

- [ ] **Step 4: 跑测试确认通过**

Run: `venv/bin/python -m pytest tests/web/ -q`
Expected: 全绿

- [ ] **Step 5: Commit(需用户确认)**

```bash
git add server/web/static/ tests/web/test_static.py
git commit -m "feat: Band Activity decode rows and CQ loop countdown"
```

---

## Task 10: 文档同步(SDD / AGENTS / tests README)

**Files:**
- Modify: `SDD/10-service-model.md`(REST `/operation/cq` loop 字段、快照 `cq_loop`、设置项、批次 dt/freq/to_me)
- Modify: `SDD/15-*.md`(第 15 章:循环的租约门控/STOP 优先/故障不重布防;TX 驱动的 slot 奇偶与单报文节奏)
- Modify: `SDD/11-*.md`(§11.3 组合根:tx_driver、cq_loop、落库 watchdog)
- Modify: `SDD/14-version-history.md`(新条目)
- Modify: `tests/README.md`(新套件清单)
- Modify: `AGENTS.md`(模块表:dsp_encode/tx_driver/cq_loop/qso_log)

- [ ] **Step 1: 核对 SDD 对应章节现状,逐节做最小修订**

Run: `python3 .agents/skills/sdd-guardian/harness/sdd_context.py brief server/engine/cq_loop.py server/engine/tx_driver.py --task "CQ loop + TX driver"`
按 §10.1/§10.2/§11.3/§15 的实际小节结构插入上述行为描述;不改任何既有不变量表述。

- [ ] **Step 2: SDD/14 新条目**

在 Unreleased 段追加(措辞参照现有条目风格):

```markdown
- Added the production TX path: `dsp_encode.SupervisorEncoder` (Protocol v1 encode over one reused TX segment), the slot-parity `TxDriver` (one sequencer message per eligible slot, encode → gated `safety.transmit`, failures counted and left to the fault matrix), and watchdog-polled QSO log wiring into the canonical store.
- Added `CqLoopController`: DONE re-arms CQ and resets the idle timer, retry-exhaustion/partner-loss re-arms without resetting, manual/fault disarm, lease loss and the configurable idle timeout (60–3600 s, default 600) stop the loop; loop state rides the state snapshot and every transition is audited. The CQ REST intent accepts `{"loop": true}`; TX off/STOP stop the loop through the existing paths.
- Upgraded the candidate pane to Band Activity rows (UTC/SNR/dt/freq/text, CQ bold, addressed-to-me highlight, late dimmed, double-click Reply through the gated path); decode batches now carry `dt`/`freq`/`to_me`; shell cache bumped to v7.
```

- [ ] **Step 3: 全量验证**

Run: `venv/bin/python -m pytest tests/ -q`
Expected: 全绿
Run: `python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged`
Expected: clean

- [ ] **Step 4: Commit(需用户确认)**

```bash
git add SDD/ tests/README.md AGENTS.md
git commit -m "docs: sync SDD and inventories for TX path, CQ loop, Band Activity"
```

---

## 最终验证(全部任务完成后)

- `venv/bin/python -m pytest tests/ -q` 全绿。
- `sdd_context.py check --staged` clean。
- 硬件验收(用户执行):开循环观察 无应答续呼 / 应答自动 QSO / QSO 后再 CQ / 空闲超时停 / STOP 立即停 / 断网 15 秒 dead-man 停;对照 acceptance/real_radio.py 的 PTT 时序。
