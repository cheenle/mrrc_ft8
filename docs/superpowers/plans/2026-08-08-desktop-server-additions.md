# Desktop Client — Server Additions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Two additive, non-breaking server changes that enable the desktop FT8 client: a `/desktop` static mount (serving the adapted FT8web build) and a `last_tx` field in the state snapshot (so the client's Active QSO panel can show outgoing TX messages).

**Architecture:** The mobile PWA (`server/web/static/`) and its contract are untouched. `TxDriver` grows an optional `on_transmitted` callback; the composition layer wires it to record the last transmitted message on `AppState` and publish a state snapshot. A new `/desktop` mount serves `desktop/ft8web/dist` (with a placeholder until the client is built).

**Tech Stack:** Python 3.11+, FastAPI, Starlette StaticFiles, pytest.

## Global Constraints

- Do not modify `server/web/static/` or any existing REST/WS contract shape the mobile PWA relies on.
- All additions must be backward-compatible: new fields/callbacks only, defaults `None`, unknown keys ignored by existing clients.
- `TxDriver.on_transmitted` must be an optional field (default `None`) so existing `TxDriver` constructions and tests are unaffected.
- Run tests with `OMP_STACKSIZE=10M venv/bin/python -m pytest tests/` from the repo root.

---

### Task 1: `TxDriver.on_transmitted` hook

**Files:**
- Modify: `server/engine/tx_driver.py`
- Test: `tests/engine/test_tx_driver.py`

**Interfaces:**
- Produces: `TxDriver.on_transmitted: Callable[[int, str, float], None] | None = None` (dataclass field). Fired with `(slot_id, message, tx_frequency_hz)` after a **successful** `safety.transmit(waveform)`; never fired on `TxRefused` refusal or encode failure.

- [ ] **Step 1: Write the failing tests**

Append to `tests/engine/test_tx_driver.py`:

```python
def test_on_transmitted_fires_after_successful_transmit() -> None:
    sequencer, driver = make_driver(FakeEncoder(), FakeSafety())
    recorded: list[tuple[int, str, float]] = []
    driver.on_transmitted = lambda slot_id, message, freq: recorded.append(
        (slot_id, message, freq)
    )
    sequencer.start_cq()
    run(driver.on_slot_start(0))
    assert recorded == [(0, "CQ M0XX IO91", 1500.0)]


def test_on_transmitted_not_fired_on_encode_failure() -> None:
    sequencer, driver = make_driver(FakeEncoder(error=WorkerFault("boom")), FakeSafety())
    recorded: list[tuple[int, str, float]] = []
    driver.on_transmitted = lambda slot_id, message, freq: recorded.append(
        (slot_id, message, freq)
    )
    sequencer.start_cq()
    run(driver.on_slot_start(0))
    assert recorded == []


def test_on_transmitted_not_fired_on_refusal() -> None:
    sequencer, driver = make_driver(FakeEncoder(), FakeSafety(error=TxRefused("test")))
    recorded: list[tuple[int, str, float]] = []
    driver.on_transmitted = lambda slot_id, message, freq: recorded.append(
        (slot_id, message, freq)
    )
    sequencer.start_cq()
    run(driver.on_slot_start(0))
    assert recorded == []
```

Note: `FakeSafety(error=...)` in `tests/engine/test_tx_driver.py` raises its error from `transmit()`, which the driver catches as `TxRefused`. `TxRefused("test")` is valid — the existing suite already constructs it as `TxRefused("not armed")` (see `tests/engine/test_tx_driver.py:79`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/engine/test_tx_driver.py -v`
Expected: the three new tests FAIL (no `on_transmitted` attribute on the dataclass).

- [ ] **Step 3: Implement the hook**

In `server/engine/tx_driver.py`:

Add to the `TxDriver` dataclass fields (after `counters`):

```python
    counters: dict[str, int] = field(
        default_factory=lambda: {"tx_attempts": 0, "tx_failed": 0}
    )
    # Optional observer for the composition layer (desktop client shows the
    # transmitted message live).  Fired only after a successful transmit.
    on_transmitted: Callable[[int, str, float], None] | None = None
```

In `_transmit`, inside the `try`, immediately after `await self.safety.transmit(waveform)`:

```python
            await self.safety.transmit(waveform)
            if self.on_transmitted is not None:
                self.on_transmitted(slot_id, message, self.sequencer.tx_frequency)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/engine/test_tx_driver.py -v`
Expected: all tests PASS (new + existing).

- [ ] **Step 5: Commit**

```bash
git add server/engine/tx_driver.py tests/engine/test_tx_driver.py
git commit -m "feat(tx): on_transmitted observer hook for last-tx display"
```

---

### Task 2: `AppState.last_tx` + state-snapshot field

**Files:**
- Modify: `server/web/api.py`
- Test: `tests/web/test_api.py`

**Interfaces:**
- Consumes: `TxDriver.on_transmitted` (Task 1).
- Produces: `AppState.last_tx: dict[str, Any] | None = None`; `_snapshot(state, session)` returns `"last_tx": state.last_tx` (additive key; existing clients ignore it).

The payload shape for `last_tx`:

```python
{
    "slot_id": int,
    "utc": "HHMMSS",       # slot start UTC
    "text": str,           # e.g. "CQ M0XX IO91"
    "freq_hz": float,      # audio offset the waveform transmitted on
}
```

- [ ] **Step 1: Write the failing test**

Append to `tests/web/test_api.py`:

```python
def test_state_snapshot_includes_last_tx(state: AppState, client: TestClient) -> None:
    login(client)
    state.last_tx = {
        "slot_id": 42,
        "utc": "071030",
        "text": "CQ M0XX IO91",
        "freq_hz": 1500.0,
    }
    response = client.get("/api/v1/state")
    assert response.status_code == 200
    body = response.json()["ok"]
    assert body["last_tx"] == state.last_tx
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/web/test_api.py::test_state_snapshot_includes_last_tx -v`
Expected: FAIL (`last_tx` missing from the response).

- [ ] **Step 3: Implement**

In `server/web/api.py`:

Add to the `AppState` dataclass (near `radio_freq_hz`):

```python
    radio_freq_hz: int | None = None  # last polled dial frequency, if rig is up
    last_tx: dict[str, Any] | None = None  # last transmitted message (desktop client)
```

In `_snapshot(state, session)` (the dict literal), add after `"radio"`:

```python
        "radio": {"freq_hz": state.radio_freq_hz},
        "last_tx": state.last_tx,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/web/test_api.py::test_state_snapshot_includes_last_tx -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/web/api.py tests/web/test_api.py
git commit -m "feat(state): last_tx in snapshot for desktop Active QSO display"
```

---

### Task 3: Wire `on_transmitted` in the composition layer

**Files:**
- Modify: `server/main.py`

**Interfaces:**
- Consumes: `AppState.last_tx` (Task 2), `TxDriver.on_transmitted` (Task 1), `_snapshot` (already imported at `server/main.py:51`).
- Produces: after every successful transmit, `state.last_tx` is set and a state snapshot is published on `state_broadcast`.

- [ ] **Step 1: Add the wiring helper**

Add a module-level helper after `_static_dir()` (or near other helpers):

```python
def _record_last_tx(state: AppState, slot_id: int, message: str, freq_hz: float) -> None:
    """Record the just-transmitted message and push a state snapshot.

    The desktop client's Active QSO panel renders outgoing messages from
    this field; the mobile PWA ignores the unknown ``last_tx`` key.
    """
    import time

    state.last_tx = {
        "slot_id": slot_id,
        "utc": time.strftime("%H%M%S", time.gmtime(slot_id * 15.0)),
        "text": message,
        "freq_hz": freq_hz,
    }
    state.bump()
    if state.state_broadcast is not None:
        state.state_broadcast.publish(_snapshot(state, None))
```

- [ ] **Step 2: Wire both `TxDriver` constructions**

At `server/main.py:437` (DSP path), after `state.tx_driver.on_tx_error = ...`:

```python
        state.tx_driver.on_transmitted = lambda slot_id, message, freq_hz: _record_last_tx(
            state, slot_id, message, freq_hz
        )
```

At `server/main.py:708` (Null-encoder path), after `state.tx_driver.on_tx_error = ...`:

```python
        state.tx_driver.on_transmitted = lambda slot_id, message, freq_hz: _record_last_tx(
            state, slot_id, message, freq_hz
        )
```

- [ ] **Step 3: Verify existing tests still pass**

Run: `venv/bin/python -m pytest tests/engine/test_tx_driver.py tests/web/test_api.py tests/web/test_main.py -v`
Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add server/main.py
git commit -m "feat(main): wire last-tx recording and snapshot publish on TX"
```

---

### Task 4: Mount `/desktop` serving the client dist

**Files:**
- Create: `desktop/ft8web/dist/index.html` (placeholder, will be overwritten by the client build)
- Modify: `server/main.py`
- Test: `tests/web/test_main.py`

**Interfaces:**
- Produces: `/desktop` (and `/desktop/`) serves the built client from `desktop/ft8web/dist` with the same no-cache policy as `/static`. Mount is conditional: if the dist dir is absent, the server still boots (mobile PWA unaffected).

- [ ] **Step 1: Write the failing test**

Append to `tests/web/test_main.py`:

```python
def test_desktop_mount_serves_client() -> None:
    app = create_server(make_config(), start_dsp=False, start_audio=False)
    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/desktop/")
        assert response.status_code == 200
        assert "MRRC-FT8 Desktop" in response.text
```

- [ ] **Step 2: Create the placeholder dist index**

Create `desktop/ft8web/dist/index.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>MRRC-FT8 Desktop</title>
</head>
<body>
  <h1>MRRC-FT8 Desktop</h1>
  <p>Desktop client placeholder — build desktop/ft8web to replace this page.</p>
</body>
</html>
```

- [ ] **Step 3: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/web/test_main.py::test_desktop_mount_serves_client -v`
Expected: FAIL (`/desktop/` returns 404).

- [ ] **Step 4: Implement the mount**

In `server/main.py`, after the `/static` mount at line 973:

```python
    app.mount("/static", _NoCacheStaticFiles(directory=_static_dir_v), name="static")

    _desktop_dir_v = _desktop_dist_dir()
    if os.path.isdir(_desktop_dir_v):
        app.mount(
            "/desktop",
            _NoCacheStaticFiles(directory=_desktop_dir_v, html=True),
            name="desktop",
        )
    return app
```

Add the helper next to `_static_dir()`:

```python
def _desktop_dist_dir() -> str:
    from pathlib import Path

    return str(Path(__file__).resolve().parent.parent / "desktop" / "ft8web" / "dist")
```

Verify `import os` is present at the top of `server/main.py` (it is — used elsewhere; if not, add it).

- [ ] **Step 5: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/web/test_main.py::test_desktop_mount_serves_client -v`
Expected: PASS.

- [ ] **Step 6: Full regression + commit**

Run: `venv/bin/python -m pytest tests/ -q`
Expected: all PASS (existing + new).

```bash
git add server/main.py desktop/ft8web/dist/index.html tests/web/test_main.py
git commit -m "feat(server): serve desktop client build at /desktop"
```
