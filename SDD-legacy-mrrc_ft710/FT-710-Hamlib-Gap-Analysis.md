# FT-710: wfview vs Hamlib Gap Analysis & Implementation Plan

## Executive Summary

wfview's Yaesu FT-710 support covers core radio control (frequency, mode, PTT, meters, basic settings) and scope/waterfall display via FT4222 SPI. However, compared to Hamlib's mature FT-710 implementation and the official FT-710 CAT specification, there are **3 critical bugs**, **8 silent ExpertSdrView UI failures**, and **~25 missing CAT commands** across several feature categories.

---

## 1. CRITICAL BUGS (3)

### 1.1 Tuner (AC) Command: Send Format Mismatch

**File:** `src/radio/yaesucommander.cpp`, lines 1843–1862  
**Severity:** HIGH — ATU on/off/tune is broken for FT-710

**Problem:** The AC command uses a P1P2P3 format where:
- P1 is always 0
- P2 = ATU type (0=bypass, 1=standard, 2=ATAS)
- P3 = Tuning state (0=not tuning, 1=tuning)

The parse side correctly interprets the radio's response:
```cpp
// Line 840-863: Radio sends "010" → parsed as decimal 10 → maps to wfview value 1 (ATU on)
// Radio sends "011" → parsed as decimal 11 → maps to wfview value 2 (tuning)
```

But the **send side** is WRONG:
```cpp
case 1:  // ATU on — should send "010" but sends "001"
    payload.append(QString::number(1).rightJustified(3, QChar('0')).toLatin1());  // "001" → WRONG!
    // payload.append(QString::number(10).rightJustified(3, QChar('0')).toLatin1()); // "010" → CORRECT (commented out!)
    break;
case 2:  // ATU tune — should send "011" but sends "003"
    payload.append(QString::number(3).rightJustified(3, QChar('0')).toLatin1());  // "003" → WRONG!
    // payload.append(QString::number(11).rightJustified(3, QChar('0')).toLatin1()); // "011" → CORRECT (commented out!)
    break;
```

**Comparison with Hamlib:** Hamlib uses the correct P1P2P3 format — `AC010;` for ATU on, `AC011;` for tune start.

**Fix:** Uncomment the correct values and delete the wrong ones:
```cpp
case 1: payload.append(QString::number(10).rightJustified(3, QChar('0')).toLatin1()); break;  // "010"
case 2: payload.append(QString::number(11).rightJustified(3, QChar('0')).toLatin1()); break;  // "011"
```

> **Note — mrrc_ft710 (this codebase) deliberately differs:** `cat_controller.py::set_tuner()` maps tuner control to `AC000/AC001/AC003` (P2=0, P3=state), citing the FT-710 CAT spec that P2=1 is invalid for the standard tuner, and SDD V1.3 records this mapping as empirically corrected against the real radio. That contradicts the Hamlib-derived `AC010;`/`AC011;` recommendation above. The discrepancy is unresolved — re-verify against actual FT-710 hardware before changing either side.

---

### 1.2 Hardware Flow Control Causes Communication Failures

**File:** `src/radio/yaesucommander.cpp`, line 101  
**Severity:** HIGH — May cause intermittent or complete serial communication failure

**Problem:**
```cpp
qobject_cast<QSerialPort*>(port)->setFlowControl(QSerialPort::HardwareControl);
```

The FT-710's Silicon Labs CP210x USB-UART bridge does not properly support RTS/CTS hardware flow control. Hamlib found the same issue with the FTdx10 and explicitly **removed serial handshake**:
> "Change FTDX10 to no serial handshake" — Hamlib 4.5.5 changelog

**Fix:**
```cpp
qobject_cast<QSerialPort*>(port)->setFlowControl(QSerialPort::NoFlowControl);
```

---

### 1.3 Missing Inter-Command Delay

**File:** `src/cachingqueue.cpp`, `src/radio/yaesucommander.cpp`  
**Severity:** MEDIUM — Can cause command drops or garbled responses under heavy polling

**Problem:** Hamlib added a **20ms post_write delay** specifically for the FT-710:
> "Increase post_write to 20ms for FT710" — Hamlib 4.5.5

wfview has no equivalent inter-command delay. The `cachingQueue` processes commands as fast as possible, which can overwhelm the FT-710's CAT processor, especially during initial state sync when many commands are sent in rapid succession.

**Fix:** Add a `QThread::msleep(20)` or timer-based delay between consecutive serial writes in `dataForRig()` or `cachingQueue::process()`.

---

## 2. SILENT ExpertSdrView UI FAILURES (8 functions)

The ExpertSdrView sends these commands via `cachingQueue`, but **NONE of them have corresponding CAT command definitions in `FT-710.rig`**. The `getCommand()` function silently deletes them:

| Button | wfview func | Missing CAT Command | FT-710 CAT Equivalent |
|--------|------------|---------------------|-----------------------|
| **RX2** | `funcVFODualWatch` | Dual Watch | No direct CAT — use VFO-B select + audio routing |
| **BS** | `funcScopeOnOff` | Scope On/Off | `SS01;` (Scope On/Off via SS command) |
| **RX.ANT** | `funcRXAntenna` | RX Antenna | `AN1;` / `AN2;` (Antenna Select) |
| **B.M** | `funcMemoryMode` | Memory Mode Toggle | No direct CAT — VFO/MEM tracked via IF command P1 field |
| **XVTR** | `funcTransverter` | Transverter | `EX` menu setting (group 01, item 11) |
| **Drive** | `funcDriveGain` | Drive/AM Carrier | `PC` (RF Power) already exists, but Drive is different |
| **ANT▼** | `funcAntenna` | Antenna Select | `AN` (Antenna Number) |
| **AGC▼** | `funcAGCTimeConstant` | AGC Time Constant | `GT` (AGC Function) |

**Impact:** The buttons appear functional in the UI but do absolutely nothing when clicked. The user gets no error feedback.

**Fix:** Add the following commands to `FT-710.rig`:

```ini
; Scope On/Off — SS01 P1; P1=0(OFF),1(ON)
Commands\77\Type=Scope On/Off
Commands\77\String=SS01
Commands\77\Min=0
Commands\77\Max=1
Commands\77\Bytes=1
Commands\77\PadRight=false
Commands\77\Command29=false
Commands\77\GetCommand=true
Commands\77\SetCommand=true
Commands\77\Admin=false

; Antenna Select — AN
Commands\78\Type=Antenna
Commands\78\String=AN
Commands\78\Min=1
Commands\78\Max=3
Commands\78\Bytes=1
Commands\78\PadRight=false
Commands\78\Command29=false
Commands\78\GetCommand=true
Commands\78\SetCommand=true
Commands\78\Admin=false

; AGC Function — GT
Commands\79\Type=AGC Time Constant
Commands\79\String=GT
Commands\79\Min=0
Commands\79\Max=3
Commands\79\Bytes=1
Commands\79\PadRight=false
Commands\79\Command29=false
Commands\79\GetCommand=true
Commands\79\SetCommand=true
Commands\79\Admin=false
```

For `funcDriveGain`: The FT-710 uses PC for RF power (5-100W). The "Drive" concept on SDR views typically maps to RF power control. The existing `RF Power` command (PC) should be used. ExpertSdrView should be updated to use `funcRFPower` instead of `funcDriveGain` for Yaesu rigs.

For `funcVFODualWatch`, `funcMemoryMode`, `funcRXAntenna`, `funcTransverter`: These need Yaesu-specific implementations or should be hidden/disabled for FT-710 when the command is not supported.

---

## 3. MISSING CAT COMMANDS (25)

The following FT-710 CAT commands from the official specification are not defined in `FT-710.rig`:

### 3.1 High Priority (affects core functionality)

| CAT Cmd | Function | wfview func | Importance |
|---------|----------|------------|------------|
| **AI** | Auto Information | `funcAutoInformation` | CRITICAL — enables unsolicited state updates from radio, reducing polling load |
| **IF** | Information (parsed but not leveraged for polling) | `funcVFOAInformation` | HIGH — combined freq+mode+S-meter in one query, far more efficient than per-field polling |
| **GT** | AGC Function | `funcAGCTimeConstant` | HIGH — AGC speed control (needed for ExpertSdrView AGC▼) |
| **RI** | Radio Information | `funcTransceiverId` (partial) | HIGH — returns full model ID string for better model detection |
| **MS** | Meter SW (switch) | — | MEDIUM — allows selecting which meter is displayed on the radio |
| **AN** | Antenna Select | `funcAntenna` | HIGH — needed for ExpertSdrView ANT▼ |
| **SS01** | Scope On/Off | `funcScopeOnOff` | HIGH — needed for ExpertSdrView BS button |

### 3.2 Medium Priority (DSP/audio quality features)

| CAT Cmd | Function | wfview func |
|---------|----------|------------|
| **DN** | ⚠ **NOT DNR** — `DN;` is the active-VFO **step-DOWN** command (~20 Hz/step). Sending it as a "DNR query" slowly walks the receive frequency downward. The `FT-710.rig` mapping `Type=DNR, String=DN` is incorrect; do not poll or send `DN;`. (DNR level is not exposed via `DN`; `RL;` is also rejected by the radio.) | — |
| **CO** | Contour | — |
| **IS** | IF Shift (different from PBT) | `funcPBTInner` (unparsed) |
| **NA** | Narrow | — |
| **CT** | CW Timing | — |
| **LK** | Lock | `funcLockFunction` |
| **VD** | VOX Delay Time | — |
| **SD** | Semi Break-In Delay | `funcBreakInDelay` |

### 3.3 Lower Priority (convenience/utility)

| CAT Cmd | Function | Notes |
|---------|----------|-------|
| **BD/BU** | Band Down/Up | Can be synthesized via BS |
| **CH** | Channel Up/Down | Memory navigation |
| **CS** | CW Spot | Tune-to-signal |
| **DA** | Dimmer | Display brightness |
| **ED/EU** | Encoder Down/Up | VFO knob emulation |
| **EK** | Enter Key | Menu navigation |
| **EX** | Menu Settings | Full menu system access |
| **FS** | Fast Step | Tuning step speed |
| **KM** | Keyer Memory | CW keyer text memories |
| **KR** | Keyer Repeat | CW message repeat |
| **LM** | Load Message | Voice/CW message |
| **OS** | Offset (Repeater Shift) | VHF/UHF repeater |
| **SC** | Scan | Scan start/stop |
| **VT** | VC Tune | Variable capacitor tune |
| **ZI** | Zero In | CW auto zero-beat |
| **SY** | Sync | VFO sync mode |
| **TS** | TX Specific | TX power by band |

---

## 4. PBT/IF Shift Parsing Gap

**File:** `src/radio/yaesucommander.cpp`, lines 784–786  
**Severity:** LOW

```cpp
case funcPBTInner:
case funcPBTOuter:
    break;  // Data is received but discarded — never stored in queue
```

Hamlib maps PBT/IF Shift to `RIG_LEVEL_IFSHIFT` with proper granularity. wfview receives the data but discards it entirely.

**Fix:** Parse the numeric value and store it via `value.setValue<short>(d.toShort())`.

---

## 5. AI (Auto Information) Mode Not Enabled

**File:** `include/yaesucommander.h`, line 120; `src/radio/yaesucommander.cpp`, line 1778  
**Severity:** MEDIUM

The `aiModeEnabled` flag exists but is **never set to `true`** anywhere in the code. The scope on/off command branches on this flag:
```cpp
if (aiModeEnabled)  // NEVER true
    value.setValue(uchar(4));  // Scope with AI
else
    value.setValue(uchar(5));  // Scope without AI
```

When AI mode is enabled on the FT-710, the radio automatically sends IF (Information) packets whenever frequency/mode changes, eliminating the need for periodic polling. Hamlib leverages this for more responsive state tracking.

**Fix:** During `commSetup()`, send `AI1;` to enable Auto Information mode. The `funcVFOAInformation` case already exists in parseData() to handle IF packets. This would also improve scope performance (value 4 vs 5).

---

## 6. COMMAND FORMAT & .RIG FILE ISSUES

### 6.1 AB Command — Missing Response Handling
The `VFO Equal AB` command (AB;) is set-only. Hamlib handles the case where it sends AB and then re-reads VFO-B frequency. wfview's implementation may miss the frequency update.

### 6.2 Scope Commands Beyond Basic
The FT-710.rig defines SS00 (speed), SS05 (span), and SS06 (mode), but not:
- **SS01** (Scope On/Off) — covered in section 2
- **SS02** (Scope Reference Level)
- **SS03** (Scope Edge Frequency)
- **SS04** (Scope Center Frequency)

`funcScopeRef` and `funcScopeEdge` are referenced in code but have no FT-710.rig mapping.

### 6.3 PulseAudio/Monitor Level
Hamlib uses `ML0`/`ML1` for monitor control (same as wfview), but Hamlib also handles the `ML` read command without the suffix.

---

## 7. HAMLIB-SPECIFIC PATTERNS TO ADOPT

### 7.1 Level Granularity (`level_gran`)
Hamlib 4.6+ introduced `level_gran` for fine-grained level control. Instead of fixed min/max/step for AF Gain, RF Gain, etc., the granularity table provides per-step increments. This is not applicable to wfview's architecture but worth noting for future improvements.

### 7.2 `RIG_TARGETABLE_MODE`
Hamlib 4.7 added `RIG_TARGETABLE_MODE` for FT-710, using `MD0`/`MD1` to target specific VFOs when setting/querying mode. wfview already supports this via `funcSelectedMode`/`funcUnselectedMode` with separate commands.

### 7.3 `post_write_delay` = 20ms
Hamlib's inter-command delay for FT-710 is critical for reliability. See section 1.3.

### 7.4 No Serial Handshake
Hamlib explicitly uses no flow control for FT-710/FTdx10. See section 1.2.

### 7.5 60M Band Handling
Hamlib fixed 60M band usage for FT-710. The FT-710.rig defines 60m as band 12 (5.25–5.45 MHz), which matches the US 60m allocation. Region-specific channelization is not handled.

---

## 8. IMPLEMENTATION PRIORITIES

### Priority 1 — Critical Fixes (must fix)
1. **Tuner AC command send format** — 1 line change (uncomment correct values)
2. **Hardware flow control** — 1 line change (NoFlowControl)
3. **Scope On/Off (SS01)** — add to .rig file + parseData handler
4. **Antenna Select (AN)** — add to .rig file

### Priority 2 — ExpertSdrView Functionality (should fix)
5. **AGC Time Constant (GT)** — add to .rig file
6. **Drive Gain → RF Power mapping** — ExpertSdrView code change for Yaesu
7. **Hide/disable unsupported buttons** (RX2, RX.ANT, XVTR, B.M) when rig doesn't support them

### Priority 3 — Protocol Robustness (would improve)
8. **Inter-command delay** — add 20ms between serial writes
9. **AI mode enable** — send AI1; during setup
10. **IF command for efficient polling** — replace per-field polls with IF parsing
11. **DNR (DN) command** — add to .rig file for modern DSP control
12. **Contour (CO) command** — add to .rig file

### Priority 4 — Completeness (nice to have)
13. Add remaining CAT commands (CT, IS, NA, LK, VD, SD, FS, etc.)
14. Create FTdx10 .rig file
15. Parse PBT/IF Shift values instead of discarding

---

## 9. FILE CHANGES REQUIRED

| File | Changes |
|------|---------|
| `rigs/FT-710.rig` | Add ~10 missing commands (SS01, AN, GT, DN, CO, CT, IS, NA, LK, etc.); update periodic polling list |
| `src/radio/yaesucommander.cpp` | Fix AC send format; fix flow control; add AI enable; parse PBT; add inter-command delay |
| `include/yaesucommander.h` | Enable AI mode; add post-write timer member |
| `src/expertsdrview/expertsdrview.cpp` | Map Drive→RFPower for Yaesu; conditionally disable unsupported buttons |
| `src/cachingqueue.cpp` | Add inter-command delay support |

---

## Sources

- FT-710 CAT Operation Reference Manual (Yaesu, 2022) — [Manualslib](https://www.manualslib.com/manual/2902433/Yaesu-Ft-710.html)
- Hamlib 4.5.5 changelog — [openSUSE Build Service](https://build.opensuse.org/projects/openSUSE:Backports:SLE-15-SP7/packages/hamlib/files/hamlib.changes)
- Hamlib FT-710 commit history — [SourceForge](https://sourceforge.net/p/hamlib/mailman/hamlib-developer/)
- kd-boss/CAT Yaesu library — [GitHub](https://github.com/kd-boss/CAT)
- wfview FT-710 ExpertSDR documentation — `FT-710-ExpertSDR.md`
- wfview mrrc_ft710 Python reference implementation — `mrrc_ft710/cat_controller.py`
