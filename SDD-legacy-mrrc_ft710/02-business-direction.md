# 2. Business Direction (BUS 411)

## 2.1 Vision

Make the Yaesu FT-710 usable from any phone browser with zero app installation: open a URL, see the spectrum, hear the audio, control the radio, and safely transmit — all through a single Python process.

## 2.2 Mission

Deliver a pragmatic, browser-native remote control surface for FT-710 operation that favors direct serial CAT integration, mobile ergonomics, low latency audio, and field maintainability over heavyweight framework or native-app complexity.

## 2.3 Business Goals

| ID | Goal | Description |
|----|------|-------------|
| G1 | Mobile RX confidence | Operator can reliably listen to FT-710 and see spectrum from phone browser |
| G2 | Safe remote control | Frequency, mode, PTT, tune, gain, and DSP controls behave predictably |
| G3 | Bidirectional audio | RX audio streams to browser; TX audio from browser reaches radio |
| G4 | Minimal deployment | Single Python process serves UI, WebSockets, audio, scope, and radio bridge |
| G5 | Design continuity | SDD records implementation facts, decisions, risks, and future work |
| G6 | Visual signal awareness | Real-time waterfall + S-meter + multi-meter provide full operating context |

## 2.4 Objectives

| ID | Objective | Target | Current Status |
|----|-----------|--------|----------------|
| O1 | Full CAT control | All essential radio commands via WebSocket | Implemented |
| O2 | RX audio streaming | Browser receives continuous tagged dual-codec audio (Opus default, PCM fallback) | Implemented |
| O3 | TX audio uplink | Browser microphone → radio transmitter | Implemented |
| O4 | Real-time spectrum | Waterfall from FT4222 SPI or S-meter fallback | Implemented |
| O5 | PTT release safety | Multiple release safeguards | Implemented |
| O6 | Memory channels | Save/recall via Web UI + server persistence | Implemented |
| O7 | Multi-meter | Real-time PWR/ALC/SWR/Id/Vd displays | Implemented |
| O8 | Mobile-first UX | Optimized for touch, safe areas, one-screen operation | Implemented |

## 2.5 Strategy

| ID | Strategy | Description |
|----|----------|-------------|
| S1 | Mobile-first UI | Touch-optimized controls, safe-area support, PWA manifest |
| S2 | Direct CAT protocol | Use serial CAT directly; no Hamlib/rigctld dependency |
| S3 | Browser-native audio | RX playback via Web Audio/AudioWorklet; TX via getUserMedia |
| S4 | Small service surface | FastAPI owns static files, WebSockets, CAT, audio in one process |
| S5 | Dual-mode spectrum | FT4222 SPI when available, S-meter fallback always works |
| S6 | Document actual state | SDD distinguishes implemented from planned features |

## 2.6 Tactics

| ID | Tactic | Implementation |
|----|--------|----------------|
| T1 | Touch-and-hold PTT | `mousedown`/`touchstart` → TX; `mouseup`/`touchend` → RX |
| T2 | Fire-and-forget TX0 + poll verify | `TX0;` on release; 500ms TX-status poll + browser watchdog catch stuck keyup |
| T3 | AudioWorklet RX | Low-latency playback with jitter buffer (prebuffer 220ms, recovery 90ms) |
| T4 | Opus audio compression | ~64kbps Opus vs ~768kbps PCM — 12× bandwidth reduction |
| T5 | PyAudio FT-710 detection | Auto-detect FT-710 USB audio device by name |
| T6 | scope_pipe subprocess | Isolate FT4222 SPI I/O from asyncio event loop |
