# FT8 DSP ABI and Worker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and prove a stable FT8 C ABI over the WSJT-X 3.0.2 standard and Improved decoders, then isolate every native call in a generation-tagged supervised DSP Worker.

**Architecture:** `wsjt_core` owns vendor Fortran state and returns fixed-capacity decode batches; OpenMP callbacks append only to Fortran-owned storage. A single Worker is the sole ctypes client, while the parent exchanges bounded JSON control frames and parent-owned shared-memory sample buffers with it. The supervisor serializes requests, rejects stale generations and turns timeout/crash/protocol errors into fail-closed DSP faults.

**Tech Stack:** CMake 3.24+, gfortran-mp-13, OpenMP, FFTW3 single precision, Fortran `iso_c_binding`, C/C++, Python 3.11+, ctypes, multiprocessing `spawn`, `multiprocessing.shared_memory`, NumPy, pytest.

---

## Design and acceptance references

This plan implements SC1 and SC3; NFR-001, NFR-002, NFR-007, NFR-021, NFR-022, NFR-076, NFR-080, NFR-081 and NFR-084; AD-002, AD-003 and AD-005; and mitigates R1–R4. It closes I8 with protocol version 1 and closes I9 only after measured benchmark output exists. It does not implement audio capture, UTC dispatch policy, PTT, rigctld or Web control.

The Phase 1 fault listener is the handoff needed by SC4 and SC6, but only Phase 2 may claim actual PTT release. SC5 and SC8 belong to Phase 3, while SC9 and SC10 remain the platform acceptance gates. The M1 stop conditions below implement SDD §13.6's no-go gate; they do not claim the later success criteria early.

Hard constants for this phase:

```text
WSJT_ABI_VERSION       = 1
WSJT_PROTOCOL_VERSION  = 1
FT8_RX_RATE            = 12000
FT8_RX_SAMPLES         = 180000
FT8_TX_RATE            = 48000
FT8_TX_SAMPLES         = 606720
FT8_RESULT_CAPACITY    = 256
MAX_CONTROL_FRAME      = 65536 bytes
DEFAULT_PROFILE        = 3
THREAD_RANGE           = 1..12; 0 means Auto in configuration only
CYCLE_RANGE            = 1..3
```

The parent owns and unlinks shared memory. A decode request names one 360,000-byte int16 RX segment. An encode request names one 2,426,880-byte float32 TX segment. The worker opens, validates, uses and closes the segment but never unlinks it. One supervisor lock permits at most one in-flight native call, matching the single Fortran owner and global binding lock.

## File map

| Path | Responsibility |
|---|---|
| `dsp/CMakeLists.txt` | Build one `wsjt_core` shared library with explicit vendor/patched source manifests and no Qt dependency. |
| `dsp/cmake/standard-ft8.cmake` | Standard FT8 source manifest. |
| `dsp/cmake/improved-ft8.cmake` | Improved `ft8var` source manifest. |
| `dsp/wsjt_core.h` | Canonical ABI constants, structures, status codes and exported prototypes. |
| `dsp/wsjt_types.f90` | Fortran interoperable types/constants matching the header. |
| `dsp/wsjt_batch.f90` | Fixed 256-entry result batch and OpenMP-safe Fortran callbacks. |
| `dsp/wsjt_core_shim.f90` | Capability query, input validation, encode and standard/Improved entry points. |
| `dsp/shmem_stub.c` | Headless true-returning lock/unlock symbols required by standard decoder; process/lock ownership is external. |
| `dsp/patched/*.f90` | Seven registered copies with exact reversible include/concurrency/request-state adaptations. |
| `server/core/models.py` | Python immutable DSP request/result/config value types shared by binding and protocol. |
| `server/core/binding.py` | Sole ctypes loader, exact ABI declarations, global `RLock`, shape/rate/capacity assertions. |
| `server/core/protocol.py` | Versioned bounded JSON envelope parser and shared-memory descriptors. |
| `server/core/worker.py` | Spawn-process loop; sole runtime importer/user of binding. |
| `server/core/supervisor.py` | Parent spawn, serialized async requests, timeout/crash handling and generation rollover. |
| `tests/dsp/` | ABI, encode, standard/Improved synthetic regression and benchmark tests. |
| `tests/core/` | Binding/protocol/worker/supervisor unit and fault tests. |
| `scripts/benchmark_dsp.py` | Repeated profile/thread timing and Auto-policy evidence. |
| `artifacts/dsp-benchmark.schema.json` | Checked-in schema for untracked machine-specific benchmark output. |

### Task 1: Freeze the vendor baseline and prove the native toolchain

**Files:**
- Create: `dsp/vendor.sha256`
- Create: `dsp/toolchain.cmake`
- Create: `tests/dsp/test_vendor_policy.py`
- Create: `tests/dsp/test_toolchain_contract.py`
- Modify: `.gitignore`

- [ ] **Step 1: Write the failing vendor-integrity test**

Tree entries must be regular files. Directories are traversed, while symlinks,
FIFOs, sockets and other non-regular entries fail closed. Regular files use
relative POSIX paths sorted by their UTF-8 byte representation, so the digest
is independent of host path comparison rules.

```python
# tests/dsp/test_vendor_policy.py
import hashlib
from pathlib import Path
import stat

import pytest


ROOT = Path(__file__).parents[2]
VENDOR_ROOT = ROOT / "wsjtx-3.0.2"
VENDOR_DIGEST = ROOT / "dsp" / "vendor.sha256"


def tree_digest(root: Path, base: Path = ROOT) -> str:
    files = []
    for path in root.rglob("*"):
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        relative_path = path.relative_to(base).as_posix()
        if not stat.S_ISREG(mode):
            raise AssertionError(f"non-regular vendor entry: {relative_path}")
        files.append(path)

    files = sorted(
        files,
        key=lambda path: path.relative_to(base).as_posix().encode("utf-8"),
    )
    outer_digest = hashlib.sha256()
    for path in files:
        inner_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        relative_path = path.relative_to(base).as_posix()
        outer_digest.update(f"{inner_digest}  {relative_path}\n".encode())
    return outer_digest.hexdigest()


def test_vendor_digest_rejects_non_regular_entries(tmp_path: Path) -> None:
    regular_file = tmp_path / "source.txt"
    regular_file.write_text("vendor source")
    (tmp_path / "source-link").symlink_to(regular_file)

    with pytest.raises(AssertionError, match="non-regular vendor entry.*source-link"):
        tree_digest(tmp_path, base=tmp_path)


def test_vendor_tree_matches_approved_digest() -> None:
    expected = VENDOR_DIGEST.read_text().strip()
    actual = tree_digest(VENDOR_ROOT)

    assert actual == expected, (
        "vendor tree digest mismatch\n"
        f"expected: {expected}\n"
        f"actual: {actual}\n"
        "restore vendor; do not refresh approved digest"
    )
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `venv/bin/python -m pytest tests/dsp/test_vendor_policy.py -v`

Expected: FAIL because `dsp/vendor.sha256` does not exist.

- [ ] **Step 3: Record the approved digest and toolchain contract**

```text
# dsp/vendor.sha256
a7a562c5cbcf81442d9f8b77ebf7777c1aee4a86b8e0b32c2bcdac588d4305c4
```

```cmake
# dsp/toolchain.cmake
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
set(CMAKE_Fortran_MODULE_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/modules")

if(NOT _MRRC_FT8_TOOLCHAIN_FLAGS_APPENDED)
  string(APPEND CMAKE_Fortran_FLAGS_DEBUG " -O0 -g -fcheck=all -fbacktrace")
  string(APPEND CMAKE_Fortran_FLAGS_RELEASE " -O3")
  set(_MRRC_FT8_TOOLCHAIN_FLAGS_APPENDED TRUE)
endif()
```

```python
# tests/dsp/test_toolchain_contract.py
from pathlib import Path
import subprocess


ROOT = Path(__file__).parents[2]
TOOLCHAIN = ROOT / "dsp" / "toolchain.cmake"


def test_toolchain_preserves_flags_and_is_idempotent(tmp_path: Path) -> None:
    probe = tmp_path / "toolchain_probe.cmake"
    probe.write_text(
        f'''set(CMAKE_Fortran_FLAGS_DEBUG "-sentinel-debug")
set(CMAKE_Fortran_FLAGS_RELEASE "-sentinel-release")
include("{TOOLCHAIN.as_posix()}")
include("{TOOLCHAIN.as_posix()}")
message(STATUS "DEBUG_FLAGS=${{CMAKE_Fortran_FLAGS_DEBUG}}")
message(STATUS "RELEASE_FLAGS=${{CMAKE_Fortran_FLAGS_RELEASE}}")
'''
    )

    completed = subprocess.run(
        ["cmake", "-P", str(probe)],
        check=True,
        capture_output=True,
        text=True,
    )
    output = completed.stdout + completed.stderr
    output_lines = output.splitlines()

    assert (
        "-- DEBUG_FLAGS=-sentinel-debug -O0 -g -fcheck=all -fbacktrace"
        in output_lines
    )
    assert "-- RELEASE_FLAGS=-sentinel-release -O3" in output_lines
```

Append to `.gitignore`:

```gitignore
dsp/build/
artifacts/dsp-benchmark.json
```

- [ ] **Step 4: Run baseline and dependency probes**

Run:

```bash
venv/bin/python -m pytest \
  tests/dsp/test_vendor_policy.py \
  tests/dsp/test_toolchain_contract.py -v
gfortran-mp-13 --version
cmake --version
pkg-config --modversion fftw3f
```

Expected: all three Task 1 tests PASS; GNU Fortran 13.x, CMake 3.24+ and FFTW3f 3.x are printed. If a probe is missing, stop M1 and install the named tool rather than changing compiler or FFT ABI assumptions.

- [ ] **Step 5: Commit**

```bash
git add .gitignore dsp/vendor.sha256 dsp/toolchain.cmake tests/dsp/test_vendor_policy.py tests/dsp/test_toolchain_contract.py
git commit -m "test: freeze WSJT-X vendor baseline"
```

### Task 2: Define and smoke-test ABI version 1

**Files:**
- Create: `dsp/CMakeLists.txt`
- Create: `dsp/wsjt_core.h`
- Create: `dsp/wsjt_types.f90`
- Create: `dsp/wsjt_core_shim.f90`
- Create: `tests/dsp/abi_smoke.c`
- Create: `tests/dsp/test_abi_smoke.py`

- [ ] **Step 1: Write the failing ABI smoke test**

```python
# tests/dsp/test_abi_smoke.py
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[2]
BUILD = ROOT / "dsp/build"


@pytest.fixture(scope="module")
def built_library() -> Path:
    subprocess.run(
        ["cmake", "-S", str(ROOT / "dsp"), "-B", str(BUILD),
         "-DCMAKE_Fortran_COMPILER=gfortran-mp-13",
         "-DCMAKE_BUILD_TYPE=Release"],
        cwd=ROOT,
        check=True,
    )
    subprocess.run(["cmake", "--build", str(BUILD), "-j"],
                   cwd=ROOT, check=True)
    suffix = ".dylib" if os.uname().sysname == "Darwin" else ".so"
    library = BUILD / f"libwsjt_core{suffix}"
    assert library.is_file(), f"configured build did not create {library}"
    return library


def test_c_header_matches_fortran_abi(built_library: Path) -> None:
    subprocess.run(
        ["cc", str(ROOT / "tests/dsp/abi_smoke.c"), "-I", str(ROOT / "dsp"),
         "-L", str(BUILD), "-lwsjt_core", "-o", str(BUILD / "abi_smoke")],
        check=True,
    )
    env = os.environ | {"DYLD_LIBRARY_PATH": str(BUILD), "LD_LIBRARY_PATH": str(BUILD)}
    result = subprocess.run([str(BUILD / "abi_smoke")], env=env, check=False,
                            text=True, capture_output=True)
    assert result.returncode == 0, (
        f"abi_smoke exited {result.returncode}\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert result.stdout.strip() == "abi=1 results=256 rx=12000/180000 tx=48000/606720 profiles=0x1f"
```

The same module must gate the complete defined dynamic-export table. Use
`xcrun dyld_info -exports` on macOS to read the export trie and
`nm -D --defined-only` on Linux so undefined Fortran/OpenMP runtime imports are
not mistaken for library exports. Normalize exactly one Mach-O ABI leading
underscore, then assert the complete table without name filtering:

```python
EXPECTED_DYNAMIC_EXPORTS = {"wsjt_get_abi_info"}


def _defined_dynamic_exports(library: Path) -> set[str]:
    system = os.uname().sysname
    if system == "Darwin":
        command = ["xcrun", "dyld_info", "-exports", str(library)]
    elif system == "Linux":
        command = ["nm", "-D", "--defined-only", str(library)]
    else:
        raise AssertionError(f"unsupported ABI smoke platform: {system}")

    result = subprocess.run(command, check=True, text=True, capture_output=True)
    if system == "Darwin":
        return {
            fields[1].removeprefix("_")
            for line in result.stdout.splitlines()
            if len(fields := line.split()) >= 2 and fields[0].startswith("0x")
        }
    return {
        line.split()[-1]
        for line in result.stdout.splitlines()
        if line.split()
    }


def test_library_exports_only_capability_entry_point(built_library: Path) -> None:
    assert _defined_dynamic_exports(built_library) == EXPECTED_DYNAMIC_EXPORTS


def test_export_control_change_relinks_library(built_library: Path) -> None:
    control_name = ("wsjt_core.unexports" if os.uname().sysname == "Darwin"
                    else "wsjt_core.exports.map")
    control_file = BUILD / control_name
    assert control_file.is_file()
    library_mtime = built_library.stat().st_mtime_ns
    library_mtime_second = library_mtime // 1_000_000_000
    while time.time_ns() // 1_000_000_000 <= library_mtime_second:
        time.sleep(0.01)
    os.utime(control_file)
    assert control_file.stat().st_mtime_ns // 1_000_000_000 > library_mtime_second
    subprocess.run(["cmake", "--build", str(BUILD), "-j"],
                   cwd=ROOT, check=True)
    assert built_library.stat().st_mtime_ns > library_mtime
```

```c
/* tests/dsp/abi_smoke.c */
#include <stdio.h>
#include "wsjt_core.h"

int main(void) {
    struct wsjt_abi_info info = {0};
    int32_t status = wsjt_get_abi_info(NULL);
    if (status != WSJT_E_NULL) {
        fprintf(stderr, "NULL query status mismatch: expected=%d actual=%d\n",
                WSJT_E_NULL, status);
        return 1;
    }
    status = wsjt_get_abi_info(&info);
    if (status != WSJT_OK) {
        fprintf(stderr, "capability query status mismatch: expected=%d actual=%d\n",
                WSJT_OK, status);
        return 2;
    }
    if (info.struct_size != (int32_t)sizeof(struct wsjt_abi_info)) {
        fprintf(stderr, "abi_info size mismatch: expected=%zu actual=%d\n",
                sizeof(struct wsjt_abi_info), info.struct_size);
        return 3;
    }
    if (info.result_size != (int32_t)sizeof(struct wsjt_decode_result)) {
        fprintf(stderr, "decode_result size mismatch: expected=%zu actual=%d\n",
                sizeof(struct wsjt_decode_result), info.result_size);
        return 4;
    }
    printf("abi=%d results=%d rx=%d/%d tx=%d/%d profiles=0x%x\n",
           info.abi_version, info.result_capacity,
           info.ft8_rx_rate, info.ft8_rx_samples,
           info.ft8_tx_rate, info.ft8_tx_samples,
           info.improved_profiles);
    return 0;
}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `venv/bin/python -m pytest tests/dsp/test_abi_smoke.py -v`

Expected: FAIL because `dsp/build/libwsjt_core` and `wsjt_core.h` do not exist.
When applying the export regression to a library built without a linker
allowlist, it must instead FAIL by reporting gfortran-generated module copy,
default-initializer and vtable exports alongside `wsjt_get_abi_info`. On macOS,
an `-exported_symbol`-only build must also FAIL on the four weak-def compiler
runtime exports `___emutls_*` and `___gcc_nested_func_ptr_*`.
With a generated export-control file passed only as a linker option but absent
from `LINK_DEPENDS`, the mtime regression must FAIL because a newer control
file leaves the shared-library mtime unchanged.

- [ ] **Step 3: Add the canonical C ABI**

Create `dsp/wsjt_core.h` with these exact public definitions:

```c
#ifndef MRRC_WSJT_CORE_H
#define MRRC_WSJT_CORE_H
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif

enum { WSJT_ABI_VERSION = 1, WSJT_FT8_RX_RATE = 12000,
       WSJT_FT8_RX_SAMPLES = 180000, WSJT_FT8_TX_RATE = 48000,
       WSJT_FT8_TX_SAMPLES = 606720, WSJT_RESULT_CAPACITY = 256,
       WSJT_TEXT_BYTES = 38 };
enum wsjt_status { WSJT_OK = 0, WSJT_E_NULL = 1, WSJT_E_ABI = 2,
                   WSJT_E_RATE = 3, WSJT_E_SHAPE = 4, WSJT_E_CONFIG = 5,
                   WSJT_E_CAPACITY = 6, WSJT_E_ENCODE = 7,
                   WSJT_E_INTERNAL = 8 };
enum wsjt_flags { WSJT_FLAG_AP = 1, WSJT_FLAG_LOW_THRESHOLD = 2,
                  WSJT_FLAG_WIDE_DX = 4, WSJT_FLAG_HIDE_DUPES = 8 };

struct wsjt_abi_info {
    int32_t abi_version, struct_size, result_size, result_capacity;
    int32_t ft8_rx_rate, ft8_rx_samples, ft8_tx_rate, ft8_tx_samples;
    int32_t improved_profiles, max_threads, max_cycles, reserved;
};
struct wsjt_decode_config {
    int32_t struct_size, sample_rate, sample_count, profile;
    int32_t threads, cycles, sensitivity, flags;
    int32_t qso_progress, rx_frequency, tx_frequency, low_frequency;
    int32_t high_frequency, ap_width, utc_hhmmss, reserved;
    char my_call[13], dx_call[13], dx_grid[7], padding[3];
};
struct wsjt_decode_result {
    int64_t slot_id;
    float sync, dt, frequency, quality;
    int32_t snr, ap_type, flags, reserved;
    char text[WSJT_TEXT_BYTES];
    char padding[2];
};

int32_t wsjt_get_abi_info(struct wsjt_abi_info *out);
int32_t wsjt_ft8_encode(const char message[WSJT_TEXT_BYTES], float frequency,
                        int32_t sample_rate, float *wave, int32_t capacity,
                        int32_t *written, char sent[WSJT_TEXT_BYTES]);
int32_t wsjt_ft8_decode_standard(const int16_t *samples,
    const struct wsjt_decode_config *config, int64_t slot_id,
    struct wsjt_decode_result *results, int32_t capacity,
    int32_t *count, int32_t *overflow);
int32_t wsjt_ft8_decode_improved(const int16_t *samples,
    const struct wsjt_decode_config *config, int64_t slot_id,
    struct wsjt_decode_result *results, int32_t capacity,
    int32_t *count, int32_t *overflow);

#ifdef __cplusplus
}
#endif
#endif
```

- [ ] **Step 4: Add interoperable Fortran types and capability query**

`dsp/wsjt_types.f90` must mirror every integer width, character-array length and field order from the header using `integer(c_int32_t)`, `integer(c_int64_t)`, `real(c_float)` and `character(c_char)`. Add compile-time constants with the same values. `dsp/wsjt_core_shim.f90` initially exports only:

```fortran
function wsjt_get_abi_info(out) result(status) bind(C, name='wsjt_get_abi_info')
  use iso_c_binding
  use wsjt_types
  type(c_ptr), value :: out
  integer(c_int32_t) :: status
  type(wsjt_abi_info), pointer :: info
  type(wsjt_abi_info) :: info_probe
  type(wsjt_decode_config) :: config_probe
  type(wsjt_decode_result) :: result_probe
  if (.not. c_associated(out)) then
    status = WSJT_E_NULL
    return
  end if
  if (c_sizeof(config_probe) /= int(100, c_size_t)) then
    status = WSJT_E_ABI
    return
  end if
  call c_f_pointer(out, info)
  info = wsjt_abi_info(WSJT_ABI_VERSION, c_sizeof(info_probe), &
      c_sizeof(result_probe), &
      WSJT_RESULT_CAPACITY, WSJT_FT8_RX_RATE, WSJT_FT8_RX_SAMPLES, &
      WSJT_FT8_TX_RATE, WSJT_FT8_TX_SAMPLES, int(z'1f'), 12, 3, 0)
  status = WSJT_OK
end function
```

Keep constructors in one line only if accepted by `gfortran-mp-13`; otherwise assign each field explicitly without changing layout.

- [ ] **Step 5: Add the minimal shared-library build**

```cmake
# dsp/CMakeLists.txt
cmake_minimum_required(VERSION 3.24)
project(wsjt_core VERSION 1.0.0 LANGUAGES C CXX Fortran)
include(${CMAKE_CURRENT_SOURCE_DIR}/toolchain.cmake)
find_package(OpenMP REQUIRED COMPONENTS Fortran)
find_package(PkgConfig REQUIRED)
pkg_check_modules(FFTW3F REQUIRED IMPORTED_TARGET fftw3f)
add_library(wsjt_core SHARED wsjt_types.f90 wsjt_core_shim.f90)
target_include_directories(wsjt_core PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(wsjt_core PRIVATE OpenMP::OpenMP_Fortran PkgConfig::FFTW3F)
set_target_properties(wsjt_core PROPERTIES OUTPUT_NAME wsjt_core)

if(APPLE)
    set(WSJT_CORE_UNEXPORTED_SYMBOLS
        "${CMAKE_CURRENT_BINARY_DIR}/wsjt_core.unexports"
    )
    file(GENERATE OUTPUT "${WSJT_CORE_UNEXPORTED_SYMBOLS}" CONTENT [=[
___emutls_*
___gcc_nested_func_ptr_*
___*_MOD_*
]=])
    target_link_options(wsjt_core PRIVATE
        "LINKER:-unexported_symbols_list,${WSJT_CORE_UNEXPORTED_SYMBOLS}"
    )
    set_property(TARGET wsjt_core APPEND PROPERTY
        LINK_DEPENDS "${WSJT_CORE_UNEXPORTED_SYMBOLS}"
    )
elseif(UNIX)
    set(WSJT_CORE_VERSION_SCRIPT
        "${CMAKE_CURRENT_BINARY_DIR}/wsjt_core.exports.map"
    )
    file(GENERATE OUTPUT "${WSJT_CORE_VERSION_SCRIPT}" CONTENT [=[
{
    global:
        wsjt_get_abi_info;
    local:
        *;
};
]=])
    target_link_options(wsjt_core PRIVATE
        "LINKER:--version-script,${WSJT_CORE_VERSION_SCRIPT}"
    )
    set_property(TARGET wsjt_core APPEND PROPERTY
        LINK_DEPENDS "${WSJT_CORE_VERSION_SCRIPT}"
    )
endif()
```

Apple ld does not permit exported- and unexported-symbol options together, and
its exported-symbol option does not hide gfortran's linked weak-def compiler
runtime helpers. Therefore macOS uses only the generated wildcard unexports
list to hide Fortran module globals and those helpers. ELF uses the generated
version-script allowlist. In both cases the full-table pytest assertion is the
hard ABI gate: only `wsjt_get_abi_info` may remain. It will also catch new
exports introduced by future Fortran or C sources. The three future prototypes
remain declarations only.

Each link option's generated control-file variable is also appended to the
target's `LINK_DEPENDS`. The mtime regression proves that editing the generated
control artifact causes a relink rather than silently leaving a stale dynamic
export table. The module-scoped fixture explicitly configures and builds before
any ABI assertion, so a pre-existing `dsp/build` cannot make these tests pass.

- [ ] **Step 6: Configure, build and verify GREEN**

Run:

```bash
cmake --fresh -S dsp -B dsp/build -DCMAKE_Fortran_COMPILER=gfortran-mp-13 -DCMAKE_BUILD_TYPE=Release
cmake --build dsp/build -j
venv/bin/python -m pytest tests/dsp/test_abi_smoke.py -v
```

Expected: all three tests PASS, touching the platform export-control artifact
advances the shared library mtime, the C smoke output is the exact capability
line, and
the normalized complete defined dynamic-export set is exactly
`{"wsjt_get_abi_info"}`. A manual diagnostic may use
`xcrun dyld_info -exports` on macOS or `nm -D --defined-only` on Linux, but the
pytest assertion is the release gate.

- [ ] **Step 7: Commit**

```bash
git add dsp/CMakeLists.txt dsp/wsjt_core.h dsp/wsjt_types.f90 dsp/wsjt_core_shim.f90 tests/dsp/abi_smoke.c tests/dsp/test_abi_smoke.py
git commit -m "feat: define wsjt_core ABI v1"
```

### Task 3: Add standard FT8 encoding and the 48 kHz waveform invariant

**Files:**
- Create: `dsp/cmake/standard-ft8.cmake`
- Create: `dsp/shmem_stub.c`
- Create: `dsp/ft8_stdcall.f90`
- Modify: `dsp/CMakeLists.txt`
- Modify: `dsp/wsjt_core_shim.f90`
- Create: `tests/dsp/test_ft8_encode.py`
- Modify: `tests/dsp/test_vendor_policy.py`
- Modify: `tests/dsp/test_abi_smoke.py`
- Modify: `AGENTS.md`
- Modify: `SDD/11-component-model.md`
- Modify: `SDD/14-version-history.md`
- Modify: `SDD/README.md`
- Modify: `tests/README.md`
- Modify: `docs/superpowers/plans/2026-08-01-ft8-dsp-worker.md`

- [x] **Step 1: Write the failing encode test**

```python
# tests/dsp/test_ft8_encode.py
from __future__ import annotations

import ctypes as c
import os
import subprocess
from pathlib import Path

import numpy as np

ROOT = Path(__file__).parents[2]
SUFFIX = ".dylib" if os.uname().sysname == "Darwin" else ".so"


def test_encode_produces_exact_48k_ft8_waveform() -> None:
    lib = c.CDLL(str(ROOT / "dsp/build" / f"libwsjt_core{SUFFIX}"))
    wave = np.zeros(606_720, dtype=np.float32)
    written = c.c_int32()
    sent = c.create_string_buffer(38)
    status = lib.wsjt_ft8_encode(
        c.c_char_p(b"CQ K1ABC FN42"), c.c_float(1500.0), c.c_int32(48_000),
        wave.ctypes.data_as(c.POINTER(c.c_float)), c.c_int32(wave.size),
        c.byref(written), sent,
    )
    assert status == 0
    assert written.value == 606_720
    assert sent.value.decode().strip() == "CQ K1ABC FN42"
    assert np.isfinite(wave).all()
    assert 0.90 <= float(np.max(np.abs(wave))) <= 1.0
    assert np.count_nonzero(wave) > 500_000


def test_encode_rejects_wrong_rate_and_capacity() -> None:
    lib = c.CDLL(str(ROOT / "dsp/build" / f"libwsjt_core{SUFFIX}"))
    wave = np.zeros(606_720, dtype=np.float32)
    written = c.c_int32(-1)
    sent = c.create_string_buffer(38)
    args = (c.c_char_p(b"CQ K1ABC FN42"), c.c_float(1500.0))
    ptr = wave.ctypes.data_as(c.POINTER(c.c_float))
    assert lib.wsjt_ft8_encode(*args, 44_100, ptr, wave.size, c.byref(written), sent) == 3
    assert lib.wsjt_ft8_encode(*args, 48_000, ptr, wave.size - 1, c.byref(written), sent) == 6
```

Actual test setup uses a module-scoped `tmp_path_factory` fixture that performs
its own Release configure/build, then declares all seven ctypes argument types
and the `int32` result type explicitly. In addition to the abbreviated sketch
above, it checks all four nullable pointers independently, zeros `written` on
every error where that pointer is usable, initializes `sent` with a non-NUL
sentinel and requires all 38 bytes to be cleared on every failure where it is
writable, and verifies vendor-rejected `CQ <K1ABC>` returns `WSJT_E_ENCODE`.

- [x] **Step 2: Run the test to verify it fails**

Run: `venv/bin/python -m pytest tests/dsp/test_ft8_encode.py -v`

Expected: FAIL because `wsjt_ft8_encode` is not exported.

Observed RED on 2026-08-01: the fresh Task 2 library configured and built, then
fixture setup failed at `dlsym(..., wsjt_ft8_encode): symbol not found`.

- [x] **Step 3: Add the explicit standard/codec source manifest**

Create `dsp/cmake/standard-ft8.cmake` with paths rooted at `${WSJTX_LIB}`:

```cmake
set(STANDARD_FT8_SOURCES
  ${WSJTX_LIB}/crc.f90 ${WSJTX_LIB}/fftw3mod.f90
  ${WSJTX_LIB}/hashing.f90 ${WSJTX_LIB}/iso_c_utilities.f90
  ${WSJTX_LIB}/packjt.f90
  ${WSJTX_LIB}/77bit/packjt77.f90 ${WSJTX_LIB}/timer_module.f90
  ${WSJTX_LIB}/timer_impl.f90 ${WSJTX_LIB}/timer_C_wrapper.f90
  ${WSJTX_LIB}/shmem.f90 ${WSJTX_LIB}/jt65_mod6.f90
  ${WSJTX_LIB}/ft8_decode.f90 ${WSJTX_LIB}/ft8/ft8_a7.f90
  ${WSJTX_LIB}/ft8/ft8_a8d.f90 ${WSJTX_LIB}/ft8/baseline.f90
  ${WSJTX_LIB}/ft8/bpdecode174_91.f90 ${WSJTX_LIB}/ft8/chkcrc13a.f90
  ${WSJTX_LIB}/ft8/chkcrc14a.f90 ${WSJTX_LIB}/ft8/compress.f90
  ${WSJTX_LIB}/ft8/decode174_91.f90 ${WSJTX_LIB}/ft8/encode174_91.f90
  ${WSJTX_LIB}/ft8/encode174_91_nocrc.f90 ${WSJTX_LIB}/ft8/filt8.f90
  ${WSJTX_LIB}/ft8/ft8apset.f90 ${WSJTX_LIB}/ft8/ft8b.f90
  ${WSJTX_LIB}/ft8/ft8_downsample.f90 ${WSJTX_LIB}/ft8/genft8.f90
  ${WSJTX_LIB}/ft8/gen_ft8wave.f90 ${WSJTX_LIB}/ft8/get_crc14.f90
  ${WSJTX_LIB}/ft8/get_spectrum_baseline.f90 ${WSJTX_LIB}/ft8/h1.f90
  ${WSJTX_LIB}/ft8/osd174_91.f90 ${WSJTX_LIB}/ft8/subtractft8.f90
  ${WSJTX_LIB}/ft8/sync8.f90 ${WSJTX_LIB}/ft8/sync8d.f90
  ${WSJTX_LIB}/ft8/twkfreq1.f90 ${WSJTX_LIB}/ft2/gfsk_pulse.f90
  ${WSJTX_LIB}/db.f90 ${WSJTX_LIB}/determ.f90 ${WSJTX_LIB}/four2a.f90
  ${WSJTX_LIB}/chkcall.f90 ${WSJTX_LIB}/deg2grid.f90 ${WSJTX_LIB}/fmtmsg.f90
  ${WSJTX_LIB}/grid2deg.f90 ${WSJTX_LIB}/indexx.f90
  ${WSJTX_LIB}/nuttal_window.f90 ${WSJTX_LIB}/pctile.f90 ${WSJTX_LIB}/peakup.f90
  ${WSJTX_LIB}/platanh.f90 ${WSJTX_LIB}/polyfit.f90 ${WSJTX_LIB}/prog_args.f90
  ${WSJTX_LIB}/smo.f90 ${WSJTX_LIB}/smo121.f90
  ${WSJTX_LIB}/shell.f90 ${CMAKE_CURRENT_SOURCE_DIR}/ft8_stdcall.f90
  ${WSJTX_LIB}/crc13.cpp ${WSJTX_LIB}/crc14.cpp
  ${CMAKE_CURRENT_SOURCE_DIR}/shmem_stub.c)
```

`dsp/shmem_stub.c` supplies only the two symbols actually referenced by `ft8_decode.f90`:

```c
#include <stdbool.h>
bool shmem_lock(void) { return true; }
bool shmem_unlock(void) { return true; }
```

`dsp/ft8_stdcall.f90` is an equivalent extraction of `stdcall` from vendor
`lib/qra/q65/q65_set_list.f90:66-97`. The vendor compilation unit also defines
`q65_set_list`, whose `genq65` reference would pull the unrelated Q65 codec
closure into this minimal FT8 build. Keep the extracted helper independent,
register its origin and regression evidence in `AGENTS.md` and SDD chapter 11,
and do not add the Q65 closure to this manifest.
`tests/dsp/test_vendor_policy.py` extracts exactly one uniquely marked vendor
`stdcall` routine and requires the local compilation unit to remain byte-for-byte
identical; missing or duplicate markers fail closed.

Fresh-build evidence corrected five omissions in the original planned manifest,
without globbing: `fftw3mod.f90` defines the module used by `four2a.f90`;
`iso_c_utilities.f90` defines the module used by both timer implementations;
`crc13.cpp`, `shell.f90`, and the independent `ft8_stdcall.f90` define linker
symbols directly referenced by the listed FT8 sources. The original vendor
`q65_set_list.f90` candidate was removed after its unrelated `genq65` reference
proved it would pull in the Q65 codec closure.

Update CMake with `set(WSJTX_LIB "${CMAKE_CURRENT_SOURCE_DIR}/../wsjtx-3.0.2/lib")`, `include(cmake/standard-ft8.cmake)`, add `${STANDARD_FT8_SOURCES}` to the target, and add `${WSJTX_LIB}`, `${WSJTX_LIB}/ft8` to private includes. The `.cpp` sources select the CXX linker driver, which supplies the platform-correct C++ runtime; do not link `stdc++` explicitly. Do not glob the vendor tree.

- [x] **Step 4: Implement the encode entry point**

In `dsp/wsjt_core_shim.f90`, validate every pointer, rate and capacity before `c_f_pointer`; copy the C message up to NUL into a blank 37-character Fortran string; call `genft8`; call `gen_ft8wave` with `nsym=79`, `nsps=7680`, `bt=2.0`, `fsample=48000.0`, `icmplx=0`, `nwave=606720`; copy the resulting real waveform to the C output; NUL-terminate `sent`; set `written=606720`. Return `WSJT_E_RATE`, `WSJT_E_CAPACITY` or `WSJT_E_ENCODE` on the tested failures.

After all pointer/rate/capacity validation and before any failure return,
independently associate each valid output pointer: clear `written` to zero and
all 38 bytes of `sent` to NUL. Never associate an invalid pointer.

Use a 606,720-element complex scratch array because the vendor routine declares both real and complex outputs, but never expose it across the ABI.

- [x] **Step 5: Build and verify GREEN**

Run:

```bash
fresh_build=$(mktemp -d /tmp/mrrc-ft8-task3.XXXXXX)
cmake -S dsp -B "$fresh_build" \
  -DCMAKE_Fortran_COMPILER=gfortran-mp-13 -DCMAKE_BUILD_TYPE=Release
cmake --build "$fresh_build" -j
venv/bin/python -m pytest tests/dsp/test_abi_smoke.py tests/dsp/test_ft8_encode.py -v
venv/bin/python -m pytest tests/ -v
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check \
  dsp/CMakeLists.txt dsp/wsjt_core_shim.f90 dsp/cmake/standard-ft8.cmake \
  dsp/shmem_stub.c dsp/ft8_stdcall.f90 tests/dsp/test_ft8_encode.py
```

Expected: all Task 2 and Task 3 tests PASS; symbol inspection shows exactly
`wsjt_get_abi_info` and `wsjt_ft8_encode`, and waveform length is exactly 606,720.

On macOS the Task 2 unexport list retains its runtime/module patterns and adds
patterns for the actual vendor Fortran, C++, OpenMP, CRC, shmem, and timer
exports observed by `dyld_info`; `LINK_DEPENDS` continues to enforce relinking.
On Linux the version script adds `wsjt_ft8_encode` to the global allowlist and
keeps every other symbol local. The ABI test compares the full dynamic export
set without name filtering.

Observed GREEN on 2026-08-01: Task 3 encode tests 8/8, Task 2 ABI tests 3/3,
full suite 16/16, guardian clean. Waveform evidence was status 0, written
606,720, finite true, peak 1.0, and 606,719 non-zero samples. `dyld_info`
reported exactly `_wsjt_ft8_encode` and `_wsjt_get_abi_info`.

Chapter 14 records this intermediate slice as `Unreleased`; the Quick Facts
version remains V1.0. Task 10 alone may claim V1.1 after the M1 gates pass.

- [ ] **Step 6: Commit (not run: repository has no HEAD and task forbids staging/commit)**

```bash
git add dsp/cmake/standard-ft8.cmake dsp/shmem_stub.c dsp/ft8_stdcall.f90 \
  dsp/CMakeLists.txt dsp/wsjt_core_shim.f90 tests/dsp/test_ft8_encode.py \
  tests/dsp/test_abi_smoke.py tests/dsp/test_vendor_policy.py AGENTS.md \
  SDD/11-component-model.md \
  SDD/14-version-history.md SDD/README.md tests/README.md \
  docs/superpowers/plans/2026-08-01-ft8-dsp-worker.md
git commit -m "feat: encode FT8 at 48 kHz"
```

### Task 4: Return standard FT8 decodes as a fixed Fortran batch

**Files:**
- Create: `dsp/wsjt_batch.f90`
- Modify: `dsp/CMakeLists.txt`
- Modify: `dsp/wsjt_core_shim.f90`
- Create: `tests/dsp/conftest.py`
- Create: `tests/dsp/test_ft8_standard_decode.py`

- [x] **Step 1: Write the failing synthetic-decode test**

```python
# tests/dsp/conftest.py
from __future__ import annotations

import ctypes as c
import os
from pathlib import Path

import numpy as np
import pytest
from scipy.signal import resample_poly

ROOT = Path(__file__).parents[2]
SUFFIX = ".dylib" if os.uname().sysname == "Darwin" else ".so"


@pytest.fixture(scope="session")
def raw_library(tmp_path_factory: pytest.TempPathFactory) -> c.CDLL:
    build = tmp_path_factory.mktemp("ft8-dsp-build")
    subprocess.run([
        "cmake", "-S", str(ROOT / "dsp"), "-B", str(build),
        "-DCMAKE_Fortran_COMPILER=gfortran-mp-13",
        "-DCMAKE_BUILD_TYPE=Release",
    ], cwd=ROOT, check=True)
    subprocess.run(["cmake", "--build", str(build), "-j"], cwd=ROOT, check=True)
    loaded = c.CDLL(str(build / f"libwsjt_core{SUFFIX}"))
    loaded.wsjt_ft8_encode.argtypes = [
        c.c_char_p, c.c_float, c.c_int32, c.POINTER(c.c_float),
        c.c_int32, c.POINTER(c.c_int32), c.POINTER(c.c_char),
    ]
    loaded.wsjt_ft8_encode.restype = c.c_int32
    return loaded


@pytest.fixture(scope="session")
def cq_slot(raw_library: c.CDLL) -> np.ndarray:
    wave48 = np.zeros(606_720, dtype=np.float32)
    written = c.c_int32()
    sent = c.create_string_buffer(38)
    assert raw_library.wsjt_ft8_encode(
        c.c_char_p(b"CQ K1ABC FN42"), c.c_float(1500.0), 48_000,
        wave48.ctypes.data_as(c.POINTER(c.c_float)), wave48.size,
        c.byref(written), sent,
    ) == 0
    wave12 = resample_poly(wave48, 1, 4).astype(np.float32)
    slot = np.zeros(180_000, dtype=np.float32)
    slot[6_000:6_000 + wave12.size] = wave12
    return np.clip(np.rint(slot * 24_000.0), -32768, 32767).astype(np.int16)
```

```python
# tests/dsp/test_ft8_standard_decode.py
from __future__ import annotations

import ctypes as c
import numpy as np


class Config(c.Structure):
    _fields_ = [
        ("struct_size", c.c_int32), ("sample_rate", c.c_int32),
        ("sample_count", c.c_int32), ("profile", c.c_int32),
        ("threads", c.c_int32), ("cycles", c.c_int32),
        ("sensitivity", c.c_int32), ("flags", c.c_int32),
        ("qso_progress", c.c_int32), ("rx_frequency", c.c_int32),
        ("tx_frequency", c.c_int32), ("low_frequency", c.c_int32),
        ("high_frequency", c.c_int32), ("ap_width", c.c_int32),
        ("utc_hhmmss", c.c_int32), ("reserved", c.c_int32),
        ("my_call", c.c_char * 13), ("dx_call", c.c_char * 13),
        ("dx_grid", c.c_char * 7), ("padding", c.c_char * 3),
    ]


class Result(c.Structure):
    _fields_ = [
        ("slot_id", c.c_int64), ("sync", c.c_float), ("dt", c.c_float),
        ("frequency", c.c_float), ("quality", c.c_float),
        ("snr", c.c_int32), ("ap_type", c.c_int32),
        ("flags", c.c_int32), ("reserved", c.c_int32),
        ("text", c.c_char * 38), ("padding", c.c_char * 2),
    ]


def config() -> Config:
    value = Config()
    value.struct_size = c.sizeof(Config)
    value.sample_rate = 12_000
    value.sample_count = 180_000
    value.profile = 3
    value.threads = 1
    value.cycles = 1
    value.sensitivity = 2
    value.flags = 1
    value.rx_frequency = value.tx_frequency = 1500
    value.low_frequency, value.high_frequency = 200, 3000
    value.ap_width = 50
    value.utc_hhmmss = 120000
    value.my_call = b"N0CALL"
    return value


def test_standard_decode_returns_known_message(raw_library: c.CDLL, cq_slot: np.ndarray) -> None:
    out = (Result * 256)()
    count, overflow = c.c_int32(), c.c_int32()
    cfg = config()
    status = raw_library.wsjt_ft8_decode_standard(
        cq_slot.ctypes.data_as(c.POINTER(c.c_int16)), c.byref(cfg),
        c.c_int64(11_908_800), out, 256, c.byref(count), c.byref(overflow),
    )
    assert status == 0
    assert overflow.value == 0
    messages = [out[i].text.decode().strip() for i in range(count.value)]
    assert "CQ K1ABC FN42" in messages
    decoded = out[messages.index("CQ K1ABC FN42")]
    assert decoded.slot_id == 11_908_800
    assert 1400.0 <= decoded.frequency <= 1600.0


def test_standard_decode_rejects_rate_shape_and_capacity(raw_library: c.CDLL, cq_slot: np.ndarray) -> None:
    out = (Result * 256)()
    count, overflow = c.c_int32(), c.c_int32()
    cfg = config()
    ptr = cq_slot.ctypes.data_as(c.POINTER(c.c_int16))
    cfg.sample_rate = 48_000
    assert raw_library.wsjt_ft8_decode_standard(ptr, c.byref(cfg), 1, out, 256, c.byref(count), c.byref(overflow)) == 3
    cfg.sample_rate, cfg.sample_count = 12_000, 179_999
    assert raw_library.wsjt_ft8_decode_standard(ptr, c.byref(cfg), 1, out, 256, c.byref(count), c.byref(overflow)) == 4
    cfg.sample_count = 180_000
    assert raw_library.wsjt_ft8_decode_standard(ptr, c.byref(cfg), 1, out, 255, c.byref(count), c.byref(overflow)) == 6
```

- [x] **Step 2: Run the tests to verify RED**

Run: `venv/bin/python -m pytest tests/dsp/test_ft8_standard_decode.py -v`

Expected: FAIL because `wsjt_ft8_decode_standard` is not implemented.

Observed RED on 2026-08-01 from a fresh temporary configure/build: 11 tests
failed with `freshly built library is missing wsjt_ft8_decode_standard`, while
the independent struct-layout test passed. The full dynamic-export assertion
also failed because the freshly linked library still exposed only the two
Task 3 ABI functions.

- [x] **Step 3: Implement the owned result batch**

Create `dsp/wsjt_batch.f90` as a module containing `batch(WSJT_RESULT_CAPACITY)`, `batch_count`, `batch_overflow` and `batch_slot`. Provide `batch_reset(slot_id)`, `append_standard(sync,snr,dt,freq,text,nap,qual)`, `append_improved(snr,dt,freq,text,nap,qual)` and `batch_copy(out,capacity,count,overflow)`. Both append routines must execute their index allocation and write inside one named OpenMP critical region:

```fortran
!$omp critical(wsjt_batch_append)
if (batch_count < WSJT_RESULT_CAPACITY) then
  batch_count = batch_count + 1
  batch(batch_count)%slot_id = batch_slot
  batch(batch_count)%sync = sync
  batch(batch_count)%snr = snr
  batch(batch_count)%dt = dt
  batch(batch_count)%frequency = freq
  batch(batch_count)%quality = qual
  batch(batch_count)%ap_type = nap
  batch(batch_count)%flags = merge(WSJT_FLAG_AP, 0, nap /= 0)
  call copy_f_text(text, batch(batch_count)%text)
else
  batch_overflow = 1
end if
!$omp end critical(wsjt_batch_append)
```

`append_improved` calls the same body with `sync=0.0_c_float`, because the Improved vendor callback does not expose sync. Do not print results and do not invoke C or Python from either callback.

- [x] **Step 4: Implement standard decode validation and dispatch**

Add `wsjt_batch.f90` before `wsjt_core_shim.f90` in CMake. In the standard entry point:

1. Reject null pointers.
2. Require `config%struct_size == c_sizeof(config)`, sample rate 12,000, sample count 180,000 and capacity at least 256.
3. After the existing ABI/rate/shape/capacity priority, return
   `WSJT_E_CONFIG` unless QSO progress is 0–5, sensitivity is 1–3, UTC is a
   valid `HHMMSS`, and `100 <= low < high <= 4910` with a width of at least
   100 Hz. Clear count/overflow but do not map samples/results or enter vendor
   on failure. Rx/Tx frequency and AP width remain outside this Task 4 native
   validation because no vendor out-of-bounds path was established for them.
4. Map exactly 180,000 int16 samples and copy them into a local Fortran `integer(c_int16_t)` array.
5. Reset the batch with the caller's int64 slot.
6. On one `ft8_decoder`, call the vendor decode in its full-slot disk sequence
   `nzhsym=(41,47,50)`. Before each call, clear a local 180,000-int16 staged
   buffer and copy the first `min(nzhsym*3456,180000)` caller samples, exposing
   141,696, 162,432 and 172,800 samples respectively. Set `newdat=.true.` for
   every stage; keep `ndepth=config%sensitivity`, `ncontest=0`,
   `nagain=.false.`, AP from flags, frequency range/config calls copied into
   fixed-width blank-padded strings and `ldiskdat=.true._1` so vendor
   wall-clock bailout cannot truncate a complete synthetic slot.
7. Copy the batch to the caller only after the third decode stage returns.

The original single `nzhsym=50` plan was corrected from vendor evidence:
`lib/jt9a.f90:58-72` and `lib/jt9.f90:423-443` orchestrate 41→47→50 for
full-slot/disk input, while `lib/ft8_decode.f90:102-115` loads the saved `dd`
buffer for depth 2/3 during early calls. A fresh-process single stage 50 left
`dd` unloaded and produced the post-symbol RED of status 0 with an empty batch.

The callback procedure passed to the vendor decoder must call only `append_standard`.

- [x] **Step 5: Build and verify GREEN**

Run:

```bash
cmake --build dsp/build -j
venv/bin/python -m pytest tests/dsp/test_ft8_standard_decode.py -v
```

Expected: all 25 tests PASS and the decoded batch contains `CQ K1ABC FN42` near 1500 Hz.

Observed focused GREEN on 2026-08-01: fresh configure/build succeeded and all
12 standard-decode tests passed, including the known CQ, exact ABI layouts,
rate/shape/capacity errors, 99/101-byte ABI-size rejection, every nullable
pointer, deterministic failure counters and no partial result copies.
Review follow-up adds the full native invalid-config matrix and a same-process
CQ(slot A)→CQ(slot B)→next-UTC empty→CQ sequence to prove batch and vendor
saved-state isolation. The safe pre-fix RED was `utc_hhmmss=236000` returning
status 0 instead of `WSJT_E_CONFIG`; the repeated-call characterization already
passed before the validation change.
Final review evidence: Task 4 passed 25/25; Task 2–4 related tests passed 36/36
and the complete suite passed 41/41. All 12 invalid-config cases also passed in
a fresh Debug build with Fortran `-fcheck=all`, without a runtime warning or
vendor entry. An
independent Release configure/build under `/tmp` succeeded; `dyld_info` showed
exactly `wsjt_get_abi_info`, `wsjt_ft8_encode` and
`wsjt_ft8_decode_standard`, with no callback or module export. Guardian passed
the explicit Task 4 code, test and documentation paths clean. Chapter 14 keeps
this slice under `Unreleased`; SDD Quick Facts remains V1.0 until Task 10.

- [ ] **Step 6: Commit (not run: repository has no HEAD and task forbids staging/commit)**

```bash
git add dsp/wsjt_batch.f90 dsp/CMakeLists.txt dsp/wsjt_core_shim.f90 \
  tests/dsp/conftest.py tests/dsp/test_ft8_standard_decode.py \
  tests/dsp/test_abi_smoke.py tests/README.md SDD/README.md \
  SDD/11-component-model.md SDD/14-version-history.md \
  docs/superpowers/plans/2026-08-01-ft8-dsp-worker.md
git commit -m "feat: batch standard FT8 decodes"
```

### Task 5: Integrate Improved `ft8var` profiles without Python callbacks

**Files:**
- Create: `dsp/patched/encode174_91var.f90`
- Create: `dsp/patched/osd174_91var.f90`
- Create: `dsp/patched/four2avar.f90`
- Create: `dsp/patched/ft8_mod1.f90`
- Create: `dsp/patched/ft8_decodevar.f90`
- Create: `dsp/patched/ft8_downsamplevar.f90`
- Create: `dsp/patched/ft8apsetvar.f90`
- Create: `dsp/cmake/improved-ft8.cmake`
- Create: `dsp/cmake/elf-export-map.cmake`
- Create: `dsp/cmake/wsjt_core.exports.map.in`
- Create: `dsp/wsjt_partition.f90`
- Create: `dsp/wsjt_a8_gate.f90`
- Create: `dsp/wsjt_test_hooks.f90`
- Create: `dsp/wsjt_improved.f90`
- Modify: `dsp/CMakeLists.txt`
- Modify: `dsp/wsjt_core_shim.f90`
- Create: `tests/dsp/test_ft8_improved_decode.py`
- Create: `tests/dsp/test_ft8_improved_concurrency.py`
- Create: `tests/dsp/test_cmake_export_map.py`
- Create: `tests/dsp/team_limit_probe.py`
- Create: `tests/dsp/partition_probe.f90`
- Create: `tests/dsp/a8_gate_probe.f90`

- [x] **Step 1: Write the failing Improved/profile tests**

```python
# tests/dsp/test_ft8_improved_decode.py
from __future__ import annotations

import ctypes as c
import numpy as np
import pytest

from .test_ft8_standard_decode import Config, Result, config


@pytest.mark.parametrize("profile", range(5))
def test_improved_profiles_decode_known_message(raw_library: c.CDLL, cq_slot: np.ndarray, profile: int) -> None:
    cfg = config()
    cfg.profile, cfg.threads, cfg.cycles = profile, 1, 1
    out = (Result * 256)()
    count, overflow = c.c_int32(), c.c_int32()
    status = raw_library.wsjt_ft8_decode_improved(
        cq_slot.ctypes.data_as(c.POINTER(c.c_int16)), c.byref(cfg),
        c.c_int64(11_908_800), out, 256, c.byref(count), c.byref(overflow),
    )
    messages = [out[i].text.decode().strip() for i in range(count.value)]
    assert status == 0
    assert overflow.value == 0
    assert "CQ K1ABC FN42" in messages


@pytest.mark.parametrize("field,value", [("profile", 5), ("threads", 13), ("cycles", 4)])
def test_improved_rejects_out_of_range_config(raw_library: c.CDLL, cq_slot: np.ndarray, field: str, value: int) -> None:
    cfg = config()
    setattr(cfg, field, value)
    out = (Result * 256)()
    count, overflow = c.c_int32(), c.c_int32()
    status = raw_library.wsjt_ft8_decode_improved(
        cq_slot.ctypes.data_as(c.POINTER(c.c_int16)), c.byref(cfg), 1,
        out, 256, c.byref(count), c.byref(overflow),
    )
    assert status == 5
```

- [x] **Step 2: Run the tests to verify RED**

Run: `venv/bin/python -m pytest tests/dsp/test_ft8_improved_decode.py -v`

Expected: FAIL because the Improved entry point and sources are absent.

- [x] **Step 3: Create the initial three relocatable patched copies**

Copy the vendor files, then make only these replacements with `apply_patch`:

```diff
--- wsjtx-3.0.2/lib/ft8var/encode174_91var.f90
+++ dsp/patched/encode174_91var.f90
@@
-include '/lib/ft8/ldpc_174_91_c_generator.f90'
+include 'ldpc_174_91_c_generator.f90'
--- wsjtx-3.0.2/lib/ft8var/osd174_91var.f90
+++ dsp/patched/osd174_91var.f90
@@
-include '/lib/ft8/ldpc_174_91_c_generator.f90'
+include 'ldpc_174_91_c_generator.f90'
--- wsjtx-3.0.2/lib/ft8var/four2avar.f90
+++ dsp/patched/four2avar.f90
@@
-  include '/lib/fftw3.f90'
+  include 'fftw3.f90'
```

These were the initial link-only copies. The quality follow-up below adds four
race/state copies and extends two of these three; all seven final copies are
registered in SDD/AGENTS and guarded by exact reversible transformations.

- [x] **Step 4: Add the explicit Improved source manifest**

Create `dsp/cmake/improved-ft8.cmake`:

```cmake
set(IMPROVED_FT8_SOURCES
  ${CMAKE_CURRENT_SOURCE_DIR}/patched/ft8_mod1.f90 ${WSJTX_LIB}/ft8var/jt65_mod2var.f90
  ${WSJTX_LIB}/ft8var/jt65_mod5.f90 ${WSJTX_LIB}/ft8var/jt65_mod9.f90
  ${CMAKE_CURRENT_SOURCE_DIR}/patched/ft8_decodevar.f90 ${WSJTX_LIB}/ft8var/agccft8.f90
  ${WSJTX_LIB}/ft8var/baddatavar.f90 ${WSJTX_LIB}/ft8var/bpdecode174_91var.f90
  ${WSJTX_LIB}/ft8var/chkfalse8var.f90 ${WSJTX_LIB}/ft8var/chkflscallvar.f90
  ${WSJTX_LIB}/ft8var/chkgridvar.f90 ${WSJTX_LIB}/ft8var/chklong8.f90
  ${WSJTX_LIB}/ft8var/chkspecial8var.f90 ${WSJTX_LIB}/ft8var/cwfilter.f90
  ${WSJTX_LIB}/ft8var/datacor.f90 ${CMAKE_CURRENT_SOURCE_DIR}/patched/encode174_91var.f90
  ${WSJTX_LIB}/ft8var/extract_callvar.f90 ${WSJTX_LIB}/ft8var/filbigvar.f90
  ${WSJTX_LIB}/ft8var/fillhashvar.f90 ${WSJTX_LIB}/ft8var/filtersfreevar.f90
  ${CMAKE_CURRENT_SOURCE_DIR}/patched/four2avar.f90
  ${CMAKE_CURRENT_SOURCE_DIR}/patched/ft8apsetvar.f90 ${WSJTX_LIB}/ft8var/ft8bvar.f90
  ${CMAKE_CURRENT_SOURCE_DIR}/patched/ft8_downsamplevar.f90 ${WSJTX_LIB}/ft8var/ft8mf1var.f90
  ${WSJTX_LIB}/ft8var/ft8mfcqvar.f90 ${WSJTX_LIB}/ft8var/ft8svar.f90
  ${WSJTX_LIB}/ft8var/ft8sdvar.f90 ${WSJTX_LIB}/ft8var/ft8sd1var.f90
  ${WSJTX_LIB}/ft8var/genft8var.f90 ${WSJTX_LIB}/ft8var/gen_ft8wavevar.f90
  ${WSJTX_LIB}/ft8var/genft8sdvar.f90 ${WSJTX_LIB}/ft8var/msgparservar.f90
  ${CMAKE_CURRENT_SOURCE_DIR}/patched/osd174_91var.f90
  ${WSJTX_LIB}/ft8var/packjt77sdvar.f90 ${WSJTX_LIB}/ft8var/partint.f90
  ${WSJTX_LIB}/ft8var/partintft8.f90 ${WSJTX_LIB}/ft8var/rms_augapvar.f90
  ${WSJTX_LIB}/ft8var/searchcallsvar.f90 ${WSJTX_LIB}/ft8var/subtractft8var.f90
  ${WSJTX_LIB}/ft8var/sync8var.f90 ${WSJTX_LIB}/ft8var/sync8dvar.f90
  ${WSJTX_LIB}/ft8var/tone8.f90 ${WSJTX_LIB}/ft8var/tone8myc.f90
  ${WSJTX_LIB}/ft8var/tonesdvar.f90 ${WSJTX_LIB}/ft8var/twkfreq1var.f90)
```

Add the manifest and `dsp/wsjt_improved.f90` to the target, and add `${WSJTX_LIB}/ft8var` plus `${WSJTX_LIB}` to include paths so the relative include fixes resolve. Keep the source list explicit and never replace it with a glob. A missing symbol is an M1 design/build failure requiring source-dependency review and a plan amendment before proceeding.

Task 5 implementation evidence: the first fresh manifest link resolved every
vendor-source dependency but exposed `_fftwf_plan_with_nthreads`, referenced
only by the planned `ft8var/filbigvar.f90` object in this closure. On macOS the
definition is provided by `/opt/local/lib/libfftw3f_threads.dylib` (confirmed
with `nm`); MacPorts does not ship a `fftw3f_threads.pc` file. The minimal
portable supplement is therefore an explicit required `find_library` for
`fftw3f_threads` and that one additional link item. No vendor source or mode
closure is added.

- [x] **Step 5: Implement profile dispatch and OpenMP batch collection**

`dsp/wsjt_improved.f90` must:

- Copy the 180,000 int16 input samples to `ft8_mod1::dd8` as real values.
- Set `mycall`, `hiscall`, `hisgrid`, `nft8cycles`, AP/wide-DX/duplicate/low-threshold globals solely from the validated config.
- Divide `[low_frequency, high_frequency]` into exactly `threads` adjacent inclusive bands.
- Run a Fortran OpenMP parallel loop with `num_threads(threads)`; iteration `i` calls `decodevar` with `nthr=i`, the matching band, and the Fortran callback that appends to `wsjt_batch`.
- Never call C/Python from the loop.

Implement these exact profile passes:

| Profile | Passes over one complete slot |
|---|---|
| 0 | standard `nzhsym=41`, then Improved capture/pass 49 |
| 1 | standard `nzhsym=41`, standard `nzhsym=46`, then Improved capture/pass 50 |
| 2 | Improved capture/pass 48 |
| 3 | Improved capture/pass 49 |
| 4 | Improved capture/pass 50 |

The pass number controls the number of populated input samples before the tail is zeroed: use `min(180000, pass * 3456)` for Improved, matching vendor `decoder.f90`. Profiles 0/1 reuse the standard callback but return one deduplicated batch. Deduplicate only exact `(text, rounded frequency Hz, rounded dt to 0.1 s)` matches after all passes; preserve first occurrence.

- [x] **Step 6: Build and verify GREEN**

Runtime discovery: the 12-thread/100 Hz Debug regression initially failed with
stack-corrupted bounds in `sync8var`. The upstream GUI launcher sets
`OMP_STACKSIZE=10M`; repeating only with that environment established before
the process/OpenMP runtime starts made the same test pass. Task 5 sets this in
the direct-ABI fixture before NumPy/SciPy/CDLL loading. Tasks 6 and 7 must carry
the identical pre-load contract into production; do not increase the value or
set it after native imports.

Run:

```bash
cmake --build dsp/build -j
venv/bin/python -m pytest tests/dsp/test_ft8_improved_decode.py -v
```

Expected: all 8 parametrized cases PASS. If any profile cannot decode the noiseless fixture, M1 is no-go until the profile dispatch matches vendor `decoder.f90`; do not weaken the test to profile 3 only.

Observed GREEN on 2026-08-01: all 37 Improved Release tests and all 37
Improved Debug bounds-check tests passed. The complete suite passed 81/81;
vendor policy included inverse-transform byte identity for the initial three
include adaptations and the unchanged full vendor digest. Every profile
returned status 0, one known CQ and no overflow in a direct ABI check. The
fresh shared library exported exactly `wsjt_get_abi_info`, `wsjt_ft8_encode`,
`wsjt_ft8_decode_standard` and `wsjt_ft8_decode_improved`. Guardian passed the
explicit Task 5 code, test and documentation paths clean. Chapter 14 keeps
this slice under `Unreleased`; SDD Quick Facts remains V1.0 until Task 10.

Review follow-up adds per-request initialization coverage for all six even/odd
CQ, MyCall and QSO histories plus A8 request gating, and a same-CDLL
CQ→empty→CQ behavior sequence. The pre-fix RED was two focused failures:
`evencq%freq` had no initializer and the parsed `ltry_a8` assignment was the
constant `.false.`. The history reset uses vendor-compatible frequency
`6000.0`, zero time offsets and zero complex symbols across every thread slot;
A8 is enabled only when that request has AP enabled, a DX call of at least
three characters and a complete four-character grid prefix. No ABI export was
added for these white-box checks.

Quality follow-up removes the remaining shared-state assumptions from the
headless OpenMP topology. `dd8`, the saved downsample FFT cache and the
`four2avar` plan registry are thread-private; cycle 2/3 scratch is owned by each
private decoder; and the complete OSD first-use check is inside its named
critical section. Every band therefore starts from the same copied slot and
performs deterministic band-local subtraction instead of the upstream GUI's
unsynchronized shared-buffer subtraction. FFTW plans are cached for the DSP
Worker lifetime and reclaimed at process exit.

The runtime must form exactly the requested team. If OpenMP supplies fewer
threads, the C ABI returns `WSJT_E_INTERNAL=8`, leaves count/overflow cleared
and copies no partial results. A8 uses one deterministic Rx-band owner, a
barrier and an atomic near-Rx gate; AP masks are cleared and rebuilt for every
request. Result append order remains unspecified and is not sorted. Exact
partition/team/A8 native probes, a two-signal normalized-set comparison and
repeated parallel-region stress cover these contracts. The clean direct-A8
fixture is stable; the fixed weak direct-A8 fixture remains
`xfail(strict=True)` because it is not reproducible across fresh library
processes and therefore does not claim weak-signal sensitivity coverage.

`MRRC_FT8_TEST_HOOKS=ON` adds only `wsjt_test_ft8_a8d`; production retains the
four planned exports. The ELF version map is rendered from a configured
template so the optional hook cannot remain as a literal unexpanded variable.
This quality slice remains under `Unreleased`, and SDD Quick Facts remains
V1.0. Only Task 10 may make the conditional V1.1 M1 claim after all gates pass.

- [ ] **Step 7: Commit (not run: repository has no HEAD and task forbids staging/commit)**

```bash
git add dsp/patched dsp/cmake/improved-ft8.cmake dsp/wsjt_improved.f90 dsp/CMakeLists.txt dsp/wsjt_core_shim.f90 tests/dsp/test_ft8_improved_decode.py
git commit -m "feat: integrate Improved FT8 batch decoder"
```

### Task 6: Add the locked ctypes boundary and immutable Python models

**Files:**
- Create: `server/core/models.py`
- Create: `server/core/binding.py`
- Create: `tests/core/test_binding.py`
- Create: `tests/core/test_dependency_rules.py`
- Modify: `pyproject.toml`

- [x] **Step 1: Write failing validation, lock and dependency tests**

```python
# tests/core/test_binding.py
from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from server.core.binding import CoreBinding
from server.core.models import DecodeConfig


class FakeNative:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.guard = threading.Lock()

    def decode(self) -> list[object]:
        with self.guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.02)
        with self.guard:
            self.active -= 1
        return []


def test_decode_rejects_wrong_dtype_shape_and_rate() -> None:
    binding = CoreBinding.for_test(FakeNative())
    cfg = DecodeConfig.standard()
    with pytest.raises(ValueError, match="int16"):
        binding.decode(np.zeros(180_000, np.float32), cfg, slot_id=1)
    with pytest.raises(ValueError, match="180000"):
        binding.decode(np.zeros(179_999, np.int16), cfg, slot_id=1)
    with pytest.raises(ValueError, match="12000"):
        binding.decode(np.zeros(180_000, np.int16), cfg.replace(sample_rate=48_000), slot_id=1)


def test_global_lock_serializes_native_calls() -> None:
    native = FakeNative()
    binding = CoreBinding.for_test(native)
    pcm = np.zeros(180_000, np.int16)
    cfg = DecodeConfig.standard()
    threads = [threading.Thread(target=binding.decode, args=(pcm, cfg, i)) for i in range(4)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert native.max_active == 1
```

```python
# tests/core/test_dependency_rules.py
from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_only_binding_loads_ctypes_library() -> None:
    offenders = []
    for path in (ROOT / "server").rglob("*.py"):
        text = path.read_text()
        if path.name != "binding.py" and ("ctypes.CDLL" in text or "from ctypes import CDLL" in text):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_only_worker_imports_binding() -> None:
    offenders = []
    for path in (ROOT / "server").rglob("*.py"):
        text = path.read_text()
        if path.name != "worker.py" and "server.core.binding" in text:
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
```

Evidence (2026-08-01): added independent coverage for immutable/slotted models,
all array/config/ASCII bounds, full ABI and capability mismatch rejection,
native status mapping, exact C layouts/signatures, flags, safe text copying,
caller-owned encode output, four-way lock serialization and AST dependency
rules. The private fake adapter preserves the plan's no-argument
`FakeNative.decode()` without changing the production C ABI adapter.

- [x] **Step 2: Run the tests to verify RED**

Run: `venv/bin/python -m pytest tests/core/test_binding.py tests/core/test_dependency_rules.py -v`

Expected: collection FAIL because `binding.py` and `models.py` do not exist.

Evidence (2026-08-01): the focused command exited 2 during collection with
`ModuleNotFoundError: No module named 'server.core.binding'`, confirming the
failure was the missing Task 6 boundary rather than a test syntax error.

- [x] **Step 3: Define immutable Python models**

`server/core/models.py` must define:

```python
from __future__ import annotations
from dataclasses import dataclass, replace
from enum import StrEnum

FT8_RX_RATE = 12_000
FT8_RX_SAMPLES = 180_000
FT8_TX_RATE = 48_000
FT8_TX_SAMPLES = 606_720
RESULT_CAPACITY = 256

class DecodePath(StrEnum):
    STANDARD = "standard"
    IMPROVED = "improved"

@dataclass(frozen=True, slots=True)
class DecodeConfig:
    path: DecodePath = DecodePath.IMPROVED
    sample_rate: int = FT8_RX_RATE
    sample_count: int = FT8_RX_SAMPLES
    profile: int = 3
    threads: int = 1
    cycles: int = 1
    sensitivity: int = 2
    ap: bool = True
    low_threshold: bool = False
    wide_dx: bool = False
    hide_duplicates: bool = True
    qso_progress: int = 0
    rx_frequency: int = 1500
    tx_frequency: int = 1500
    low_frequency: int = 200
    high_frequency: int = 3000
    ap_width: int = 50
    utc_hhmmss: int = 0
    my_call: str = ""
    dx_call: str = ""
    dx_grid: str = ""

    @classmethod
    def standard(cls) -> "DecodeConfig":
        return cls(path=DecodePath.STANDARD)

    def replace(self, **changes: object) -> "DecodeConfig":
        return replace(self, **changes)

@dataclass(frozen=True, slots=True)
class DecodeResult:
    slot_id: int
    sync: float
    snr: int
    dt: float
    frequency: float
    text: str
    ap_type: int
    quality: float
    flags: int

@dataclass(frozen=True, slots=True)
class DecodeBatch:
    slot_id: int
    path: DecodePath
    results: tuple[DecodeResult, ...]
    overflow: bool
    elapsed_seconds: float
    deadline_missed: bool = False
```

Add `EncodeResult(message: str, sample_rate: int, sample_count: int)` for Worker metadata; the waveform remains in parent-owned shared memory. Validate config at the binding boundary, not in `__post_init__`, so malformed protocol frames can be represented and rejected with a protocol/native status.

Evidence (2026-08-01): `server/core/models.py` implements these exact constants,
defaults and frozen/slotted value types without model-level validation.

- [x] **Step 4: Implement the sole ctypes loader and lock**

Before any module-level NumPy import or first `ctypes.CDLL` call,
`server/core/binding.py` must execute
`os.environ.setdefault("OMP_STACKSIZE", "10M")`. This must run before an
OpenMP runtime can load; setting it after constructing `CDLL` is invalid.

`server/core/binding.py` must import ctypes as a module, declare `_AbiInfo`, `_DecodeConfig`, `_DecodeResult` with field order identical to `wsjt_core.h`, set every function's `argtypes/restype`, call `wsjt_get_abi_info` at construction, and reject ABI version/struct-size/capability mismatches. Define exactly one module-level lock:

```python
DSP_LOCK = threading.RLock()
```

`CoreBinding.decode(samples, config, slot_id)` validates C-contiguous one-dimensional `np.int16`, exact length/rate/capacity, profile 0–4, threads 1–12, cycles 1–3, frequency ordering and ASCII field lengths before entering `DSP_LOCK`. Under the lock it calls the standard or Improved function once, maps nonzero status to `DspStatusError`, copies results into immutable `DecodeResult` objects and returns `DecodeBatch`. `encode` similarly requires 48,000 Hz and a C-contiguous float32 output of exactly 606,720 samples.

`CoreBinding.for_test(native)` uses a private adapter that still runs through `DSP_LOCK` and validation but does not load a library. No other module receives a raw CDLL handle.

Evidence (2026-08-01): the production adapter follows `dsp/wsjt_core.h`
(48-byte ABI info, 100-byte config, 80-byte result) and configures all four
exports. Construction rejects every advertised mismatch; validated standard,
Improved and encode calls each execute once while holding the sole `DSP_LOCK`.

- [x] **Step 5: Fix package discovery for core subpackages**

Replace `[tool.setuptools] packages = ["server"]` with:

```toml
[tool.setuptools.packages.find]
include = ["server*"]
```

Evidence (2026-08-01): package discovery now includes all `server*`
subpackages; editable-install verification is recorded in Step 6.

- [x] **Step 6: Run tests and ABI checks GREEN**

Run:

```bash
venv/bin/pip install -e '.[dev]'
venv/bin/python -m pytest tests/core/test_binding.py tests/core/test_dependency_rules.py tests/dsp/ -v
```

Expected: all binding/dependency and DSP tests PASS; concurrent fake calls report `max_active == 1`.

Evidence (2026-08-01): editable installation succeeded with setuptools package
discovery including `server.core`. The exact pytest command collected 161
tests and finished with **160 passed, 1 xfailed** in 69.21 s; the sole XFAIL is
the already documented strict weak direct-A8 evidence limitation from Task 5.
The four-way fake-native test observed `max_active == 1`. A separate production
adapter smoke against `dsp/build/libwsjt_core.dylib` negotiated the ABI and
encoded the exact 606,720-sample waveform. This completes Task 6 evidence only;
it does not claim the Task 10 M1/V1.1 gate.

#### Task 6 quality follow-up — reviewer recheck pending

- [x] Added poisoned production-adapter regressions proving nonzero native
  status never consumes `count`, `overflow` or result records.
- [x] Revalidated 256-result/binary-overflow metadata in the common binding
  layer so production and `for_test` adapters cannot diverge.
- [x] Negotiated `reserved == 0`, required aligned RX/TX buffers and writeable
  TX output, and normalized oversized numeric frequency input to `ValueError`.
- [x] Compared exact ctypes field types, sizes, key offsets and all four
  signatures; both decode signatures use an independent test-owned seven-item
  literal, and a mutation probe rejects `c_int32` in the `c_int64` slot-ID
  position.
- [x] Exact-path AST regressions cover the explicitly enumerated static ctypes
  loader/library-object imports, references and aliases, ctypes star import,
  NumPy-alias `.ctypeslib.load_library`, and direct
  `numpy.ctypeslib.load_library` aliases. They prevent basename lookalikes from
  receiving an allowlist exemption, but do not claim general data-flow or
  dynamic-import coverage.

RED evidence (2026-08-01): the expanded focused suite exited 1 with **19
failed, 68 passed**. Failures independently exposed reserved negotiation,
unaligned/read-only buffers, oversized-frequency exception leakage, failed-call
output consumption, invalid success metadata, common-layer capacity/overflow
drift and missing exact-path/alias dependency helpers. After the minimal fixes,
the focused suite passed **90 tests**. The requested core+dsp command then
finished with **189 passed, 1 xfailed** in 68.79 s, and the complete `tests/`
run finished with **191 passed, 1 xfailed** in 69.35 s; the sole XFAIL remains
the Task 5 weak direct-A8 evidence limitation. A production ctypes smoke against
the built dylib completed ABI negotiation, encode and an empty-slot standard
decode. Guardian evidence is recorded by the explicit-path final gate. Task 6
readiness remains a reviewer decision; this follow-up does not make that claim
or an M1/V1.1 claim.

Reviewer-recheck RED evidence (2026-08-01): after adding only the five missing
static-loader fixtures, the focused suite exited 1 with **5 failed, 91 passed**.
The failures were exactly the ctypes loader attribute assignment, ctypes
library-object assignment, ctypes star import, NumPy module-alias loader and
direct NumPy loader-alias forms. The ABI literal and slot-ID mutation probe
already passed. After the finite AST-helper fix, the same focused suite passed
**96 tests**; the focused core suite also passed **96 tests**, and the complete
`tests/` run finished with **197 passed, 1 xfailed** in 70.11 s. The sole XFAIL
remains the Task 5 weak direct-A8 evidence limitation. Production code was
unchanged. The guardian check over both changed-test/document paths and the
explicit SDD README/version-history paths printed `SDD-GUARDIAN: clean — no
constraint violations.` Readiness remains with the same reviewer.

- [ ] **Step 7: Commit (not run: repository has no HEAD and task forbids staging/commit)**

```bash
git add pyproject.toml server/core/models.py server/core/binding.py tests/core/test_binding.py tests/core/test_dependency_rules.py
git commit -m "feat: add locked ctypes DSP binding"
```

### Task 7: Define bounded JSON/shared-memory IPC and the Worker loop

**Files:**
- Create: `server/core/protocol.py`
- Create: `server/core/worker.py`
- Create: `tests/core/test_protocol.py`
- Create: `tests/core/test_worker.py`

- [x] **Step 1: Write failing protocol and Worker integration tests**

```python
# tests/core/test_protocol.py
import json
import pytest

from server.core.protocol import MAX_CONTROL_FRAME, FrameError, decode_frame, encode_frame


def test_frame_round_trip_is_versioned_json() -> None:
    frame = {"v": 1, "type": "ping", "generation": 7, "request_id": 9}
    assert decode_frame(encode_frame(frame)) == frame


def test_frame_rejects_oversize_bad_version_and_unknown_type() -> None:
    with pytest.raises(FrameError, match="65536"):
        decode_frame(b" " * (MAX_CONTROL_FRAME + 1))
    with pytest.raises(FrameError, match="version"):
        decode_frame(json.dumps({"v": 2, "type": "ping"}).encode())
    with pytest.raises(FrameError, match="type"):
        decode_frame(json.dumps({"v": 1, "type": "evil"}).encode())
```

```python
# tests/core/test_worker.py
from __future__ import annotations

import multiprocessing as mp
from multiprocessing.shared_memory import SharedMemory
from pathlib import Path

import numpy as np

from server.core.protocol import encode_frame, decode_frame
from server.core.worker import worker_main


def test_worker_decodes_shared_memory_without_copying_control_payload(cq_slot: np.ndarray) -> None:
    ctx = mp.get_context("spawn")
    parent, child = ctx.Pipe(duplex=True)
    shm = SharedMemory(create=True, size=cq_slot.nbytes)
    try:
        np.ndarray(cq_slot.shape, dtype=np.int16, buffer=shm.buf)[:] = cq_slot
        process = ctx.Process(target=worker_main, name="dsp-worker-test", args=(child, 3, None))
        process.start()
        parent.send_bytes(encode_frame({
            "v": 1, "type": "decode", "generation": 3, "request_id": 1,
            "slot_id": 11908800, "deadline_monotonic": 999999999.0,
            "shm": {"name": shm.name, "dtype": "<i2", "shape": [180000], "nbytes": 360000},
            "config": {"path": "improved", "sample_rate": 12000,
                "sample_count": 180000, "profile": 3, "threads": 1,
                "cycles": 1, "sensitivity": 2, "ap": True,
                "low_threshold": False, "wide_dx": False,
                "hide_duplicates": True, "qso_progress": 0,
                "rx_frequency": 1500, "tx_frequency": 1500,
                "low_frequency": 200, "high_frequency": 3000,
                "ap_width": 50, "utc_hhmmss": 120000,
                "my_call": "N0CALL", "dx_call": "", "dx_grid": ""}}))
        response = decode_frame(parent.recv_bytes())
        assert response["type"] == "decode_ok"
        assert response["generation"] == 3 and response["request_id"] == 1
        assert any(item["text"] == "CQ K1ABC FN42" for item in response["results"])
        parent.send_bytes(encode_frame({"v": 1, "type": "shutdown", "generation": 3, "request_id": 2}))
        process.join(5)
        assert process.exitcode == 0
    finally:
        shm.close()
        shm.unlink()
```

- [x] **Step 2: Run the tests to verify RED**

Run: `venv/bin/python -m pytest tests/core/test_protocol.py tests/core/test_worker.py -v`

Expected: collection FAIL because protocol and Worker modules do not exist.

- [x] **Step 3: Implement exact protocol version 1**

`server/core/protocol.py` defines `PROTOCOL_VERSION=1`, `MAX_CONTROL_FRAME=65_536`, `FrameError`, `SharedMemorySpec`, `encode_frame` and `decode_frame`. Frames use UTF-8 JSON with sorted keys and compact separators; NaN/Infinity are forbidden. `decode_frame` checks size before JSON parsing, requires an object and `v==1`, and permits only:

```text
ping, pong, decode, decode_ok, encode, encode_ok, error, shutdown, stopped
```

For each type, reject missing fields, booleans where integers are expected, extra top-level fields and invalid shared-memory descriptors. Decode input must be `dtype="<i2"`, `shape=[180000]`, `nbytes=360000`; encode output must be `dtype="<f4"`, `shape=[606720]`, `nbytes=2426880`. All frames require integer generation/request IDs. Results are objects with exactly the nine `DecodeResult` fields. Error frames contain stable `code` and printable `detail`, never a traceback.

- [x] **Step 4: Implement the Worker as the sole binding importer**

At the very start of `worker_main`, before importing NumPy or importing
`server.core.binding`, execute
`os.environ.setdefault("OMP_STACKSIZE", "10M")`. The spawned process boundary
is the production enforcement point for the upstream thread-stack prerequisite.

In `server/core/worker.py`, place `from server.core.binding import CoreBinding` inside `worker_main`, after the spawned process begins. The loop:

1. Builds one binding and sends no unsolicited messages.
2. Reads with `connection.recv_bytes(MAX_CONTROL_FRAME + 1)`.
3. Validates generation before opening shared memory.
4. Opens a named segment, verifies its actual size, creates a NumPy view with the fixed dtype/shape, and calls binding synchronously.
5. Closes but never unlinks the segment in `finally`.
6. Sends one matching `decode_ok`, `encode_ok`, `pong`, `stopped` or sanitized `error` frame.
7. Exits zero after `shutdown`; exits nonzero on pipe/protocol corruption so the supervisor observes a fault.

The Worker does not know about PTT, leases, radios, Web sessions or automatic retries.

- [x] **Step 5: Run protocol and integration tests GREEN**

Run: `venv/bin/python -m pytest tests/core/test_protocol.py tests/core/test_worker.py -v`

Expected: all tests PASS; serialized decode request is under 2 KiB and contains no audio samples.

- [ ] **Step 6: Commit (not run: repository has no HEAD and task forbids staging/commit)**

```bash
git add server/core/protocol.py server/core/worker.py tests/core/test_protocol.py tests/core/test_worker.py
git commit -m "feat: add bounded DSP worker protocol"
```

### Task 8: Supervise generation, timeout, crash and stale responses

**Files:**
- Create: `server/core/supervisor.py`
- Create: `tests/core/fake_workers.py`
- Create: `tests/core/test_supervisor.py`

- [ ] **Step 1: Write failing fault/restart tests**

```python
# tests/core/fake_workers.py
import os
import time
from server.core.protocol import decode_frame, encode_frame

def hanging_worker(connection, generation, library_path):
    connection.recv_bytes()
    time.sleep(60)

def crashing_worker(connection, generation, library_path):
    connection.recv_bytes()
    os._exit(23)

def stale_worker(connection, generation, library_path):
    request = decode_frame(connection.recv_bytes())
    connection.send_bytes(encode_frame({"v": 1, "type": "pong",
        "generation": generation - 1, "request_id": request["request_id"]}))
```

```python
# tests/core/test_supervisor.py
from __future__ import annotations

import asyncio
import pytest

from server.core.supervisor import DspFault, DspSupervisor
from tests.core.fake_workers import crashing_worker, hanging_worker, stale_worker


@pytest.mark.parametrize(
    "target,code", [(hanging_worker, "timeout"), (crashing_worker, "worker_exit"),
                    (stale_worker, "stale_generation")],
)
def test_faults_fail_closed_and_require_monitor_only_restart(target, code) -> None:
    async def scenario() -> None:
        supervisor = DspSupervisor(worker_target=target, request_timeout=0.15)
        await supervisor.start(monitor_only=True)
        first_generation = supervisor.generation
        with pytest.raises(DspFault, match=code):
            await supervisor.ping()
        assert supervisor.faulted and not supervisor.healthy
        with pytest.raises(DspFault, match="monitor_only"):
            await supervisor.restart(monitor_only=False)
        await supervisor.restart(monitor_only=True)
        assert supervisor.generation == first_generation + 1
        assert supervisor.healthy
        await supervisor.close()
    asyncio.run(scenario())


def test_concurrent_requests_are_serialized() -> None:
    async def scenario() -> None:
        supervisor = DspSupervisor(request_timeout=5.0)
        await supervisor.start(monitor_only=True)
        await asyncio.gather(*(supervisor.ping() for _ in range(8)))
        assert supervisor.max_in_flight == 1
        await supervisor.close()
    asyncio.run(scenario())
```

- [ ] **Step 2: Run the tests to verify RED**

Run: `venv/bin/python -m pytest tests/core/test_supervisor.py -v`

Expected: collection FAIL because `DspSupervisor` does not exist.

- [ ] **Step 3: Implement fail-closed supervision**

`DspSupervisor` uses `multiprocessing.get_context("spawn")`, one duplex Pipe and one `asyncio.Lock`. `start(monitor_only)` and `restart(monitor_only)` reject false, increment a monotonic in-memory generation, spawn `worker_main(connection,generation,library_path)`, then verify it with `ping`. Request IDs increase within a generation and never repeat.

For each request, use `await asyncio.wait_for(asyncio.to_thread(connection.recv_bytes, MAX_CONTROL_FRAME + 1), timeout)`. Validate version, response type, generation and request ID before returning. Any timeout, EOF, nonzero process exit, malformed frame or mismatch runs `_fault(code, detail)`, which:

- marks unhealthy/faulted before invoking listeners;
- closes the Pipe;
- terminates and joins a still-running Worker with a bounded 2-second join;
- fails the current request and does not retry it;
- never calls `start` or `restart` automatically.

`decode` and `encode` allocate parent-owned shared memory in a context manager, copy/open the NumPy view, issue one request, copy results/metadata, then close and unlink in `finally`. `close()` requests graceful shutdown, then performs bounded termination cleanup if necessary. Store `in_flight`/`max_in_flight` diagnostic counters.

Accept fault listeners with signature `Callable[[DspFaultEvent], Awaitable[None]]`; Phase 2 will attach the central safe-stop handler. Listener exceptions are collected for diagnostics and cannot turn the Worker healthy again.

- [ ] **Step 4: Add deadline classification**

`decode(..., deadline_monotonic)` records dispatch/completion using `time.monotonic()`. A response completing after the supplied deadline is returned with `DecodeBatch.deadline_missed=True`; it is not discarded, retried or shifted to another slot. This phase exposes the fact; Phase 2 alone decides display-only versus TX eligibility.

- [ ] **Step 5: Run supervisor and full core tests GREEN**

Run:

```bash
venv/bin/python -m pytest tests/core/test_supervisor.py tests/core/ -v
```

Expected: timeout, exit and stale-generation cases PASS; each restart increments exactly once; max in flight is 1; no shared-memory leak warning is emitted.

- [ ] **Step 6: Commit**

```bash
git add server/core/supervisor.py tests/core/fake_workers.py tests/core/test_supervisor.py
git commit -m "feat: supervise DSP worker failures"
```

### Task 9: Prove end-to-end regression and measure profiles/Auto threads

**Files:**
- Create: `tests/core/test_dsp_end_to_end.py`
- Create: `tests/dsp/test_benchmark_schema.py`
- Create: `scripts/benchmark_dsp.py`
- Create: `artifacts/dsp-benchmark.schema.json`

- [ ] **Step 1: Write the failing Worker end-to-end regression**

```python
# tests/core/test_dsp_end_to_end.py
from __future__ import annotations

import asyncio
import time

import numpy as np
from scipy.signal import resample_poly

from server.core.models import DecodeConfig, DecodePath
from server.core.supervisor import DspSupervisor


def test_encode_and_both_decode_paths_cross_worker_boundary() -> None:
    async def scenario() -> None:
        supervisor = DspSupervisor(request_timeout=15.0)
        await supervisor.start(monitor_only=True)
        wave48, encoded = await supervisor.encode("CQ K1ABC FN42", frequency=1500.0)
        assert encoded.sample_rate == 48_000 and wave48.shape == (606_720,)
        wave12 = resample_poly(wave48, 1, 4).astype(np.float32)
        slot = np.zeros(180_000, np.float32)
        slot[6_000:6_000 + wave12.size] = wave12
        pcm = np.clip(np.rint(slot * 24_000), -32768, 32767).astype(np.int16)
        for path in (DecodePath.STANDARD, DecodePath.IMPROVED):
            cfg = DecodeConfig.standard().replace(path=path, profile=3, threads=1)
            batch = await supervisor.decode(pcm, cfg, slot_id=11_908_800,
                deadline_monotonic=time.monotonic() + 15.0)
            assert "CQ K1ABC FN42" in {result.text for result in batch.results}
            assert batch.slot_id == 11_908_800 and not batch.overflow
        await supervisor.close()
    asyncio.run(scenario())
```

- [ ] **Step 2: Write the failing benchmark-schema test**

```python
# tests/dsp/test_benchmark_schema.py
import json
from pathlib import Path
import jsonschema

ROOT = Path(__file__).parents[2]

def test_checked_in_benchmark_example_matches_schema() -> None:
    schema = json.loads((ROOT / "artifacts/dsp-benchmark.schema.json").read_text())
    required = {"schema_version", "created_utc", "platform", "cpu_count",
                "library_abi", "safe_cutoff_offset_seconds", "runs",
                "auto_threads", "profile3_safe"}
    assert set(schema["required"]) == required
    assert schema["properties"]["schema_version"]["const"] == 1
```

Add `jsonschema>=4.22` to the `dev` dependency list.

- [ ] **Step 3: Run the new tests to verify RED**

Run: `venv/bin/python -m pytest tests/core/test_dsp_end_to_end.py tests/dsp/test_benchmark_schema.py -v`

Expected: end-to-end test FAIL until supervisor encode/decode wiring is complete; schema test FAIL because the schema is absent.

- [ ] **Step 4: Define the benchmark evidence schema**

Create a Draft 2020-12 JSON schema at `artifacts/dsp-benchmark.schema.json` that requires exactly the fields named by the test. Each `runs` item requires `profile` (0–4), `threads` (1–12), `fixture` (`clean`, `snr-18`, or `snr-22`), `iterations` (minimum 10), `capture_offset_seconds`, `elapsed_seconds` array, `p50`, `p95`, `p99`, `worst`, `completion_p99_offset` and `deadline_misses`. `auto_threads` is 1–12; `profile3_safe` is boolean; no additional properties are allowed at any level.

The M1 safety threshold is `safe_cutoff_offset_seconds=14.75`, reserving the final 250 ms of the 15-second slot for main-process decision/queue jitter. Phase 2 may move the cutoff earlier after I10 hardware lead-time measurement; it may not move it later without amending NFR-001 and the safety analysis.

- [ ] **Step 5: Implement deterministic benchmark fixtures and policy**

`scripts/benchmark_dsp.py` must accept:

```text
--output artifacts/dsp-benchmark.json
--iterations 10
--profiles 0,1,2,3,4
--threads 1,2,3,4,5,6,7,8,9,10,11,12
--require-safe
```

It uses the supervisor boundary, not direct ctypes. Generate one clean slot from `CQ K1ABC FN42`, then add deterministic NumPy RNG seed `20260801` Gaussian noise for -18 dB and -22 dB relative fixtures. Measure signal RMS over samples 6,000:157,680; normalize a unit-RMS Gaussian vector over the same interval; set `noise_rms = signal_rms / 10**(snr_db/20)`; add, clip and convert to int16. Warm each profile/thread pair once, time at least 10 independent requests for each fixture, and compute percentiles with `numpy.percentile`. Capture offsets are 14.112 for profile 0, 14.400 for profile 1, 13.824 for profile 2, 14.112 for profile 3 and 14.400 for profile 4. Completion offset is capture offset plus observed supervisor round-trip elapsed time.

Auto policy is deterministic: among thread counts whose profile-3 `completion_p99_offset <= 14.75` and which decode the known message in all three fixtures, choose the smallest thread count within 5% of the lowest safe p99. If no thread count is safe, set `profile3_safe=false`, omit no data, write the report, and exit 2 when `--require-safe` is present.

- [ ] **Step 6: Run regression and produce M1 measurement**

Run:

```bash
venv/bin/pip install -e '.[dev]'
venv/bin/python -m pytest tests/core/test_dsp_end_to_end.py tests/dsp/test_benchmark_schema.py -v
venv/bin/python scripts/benchmark_dsp.py --output artifacts/dsp-benchmark.json --iterations 10 --profiles 0,1,2,3,4 --threads 1,2,3,4,5,6,7,8,9,10,11,12 --require-safe
```

Expected: both tests PASS; benchmark exits 0, writes schema-valid JSON, reports `profile3_safe: true`, a concrete `auto_threads` value and zero deadline misses for that thread count. Exit 2 is an M1 no-go, not permission to relax 14.75 seconds or silently choose profile 2.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml tests/core/test_dsp_end_to_end.py tests/dsp/test_benchmark_schema.py scripts/benchmark_dsp.py artifacts/dsp-benchmark.schema.json
git commit -m "test: benchmark FT8 DSP profiles"
```

### Task 10: Synchronize SDD, patch register and M1 verification

**Files:**
- Create: `dsp/README.md`
- Modify: `AGENTS.md`
- Modify: `tests/README.md`
- Modify: `SDD/README.md`
- Modify: `SDD/09-architecture-overview.md`
- Modify: `SDD/11-component-model.md`
- Modify: `SDD/13-feasibility-assessment.md`
- Modify: `SDD/14-version-history.md`

- [ ] **Step 1: Write the failing documentation-contract tests**

Add to `tests/test_sdd_harness.py`:

```python
def test_m1_patch_register_and_protocol_are_documented() -> None:
    root = Path(__file__).parents[1]
    chapter11 = (root / "SDD/11-component-model.md").read_text()
    agents = (root / "AGENTS.md").read_text()
    feasibility = (root / "SDD/13-feasibility-assessment.md").read_text()
    for name in (
        "encode174_91var.f90", "osd174_91var.f90", "four2avar.f90",
        "ft8_mod1.f90", "ft8_decodevar.f90", "ft8_downsamplevar.f90",
        "ft8apsetvar.f90",
    ):
        assert name in chapter11 and name in agents
    assert "protocol version 1" in chapter11
    assert "I8 | Resolved" in feasibility
    assert "I9 | Resolved" in feasibility
```

- [ ] **Step 2: Run the contract test to verify RED**

Run: `venv/bin/python -m pytest tests/test_sdd_harness.py::test_m1_patch_register_and_protocol_are_documented -v`

Expected: FAIL because the patch and I8/I9 resolution records are absent.

- [ ] **Step 3: Document the verified build and ABI**

`dsp/README.md` records prerequisites, the exact configure/build commands, ABI constants, standard/Improved entry points, Profile 0–4 table, source-manifest policy, shared-memory ownership and how to run synthetic/benchmark gates. It must say `wsjtx-3.0.2/` is immutable and direct use of the library outside the Worker is unsupported.

Update `tests/README.md` with the new `tests/dsp` and `tests/core` inventories, separating fast pure-Python tests, native synthetic tests and the non-default benchmark command.

- [ ] **Step 4: Register patched sources exactly**

In both `AGENTS.md` and SDD §11.5, add a table with these rows:

| Patched copy | Vendor origin | Difference | Reason | Regression |
|---|---|---|---|---|
| `dsp/patched/encode174_91var.f90` | `wsjtx-3.0.2/lib/ft8var/encode174_91var.f90` | Remove leading `/lib/ft8/` from one include | Relocatable headless build | Improved encode/decode build + synthetic test |
| `dsp/patched/osd174_91var.f90` | `wsjtx-3.0.2/lib/ft8var/osd174_91var.f90` | Relative LDPC include and complete first-use check inside named critical | Relocatable build and race-free initialization | Exact reversible patch + parallel stress |
| `dsp/patched/four2avar.f90` | `wsjtx-3.0.2/lib/ft8var/four2avar.f90` | Relative FFTW include and thread-private plan registry | Relocatable FFTW and per-thread cache | Exact reversible patch + repeated parallel regions |
| `dsp/patched/ft8_mod1.f90` | `wsjtx-3.0.2/lib/ft8var/ft8_mod1.f90` | `dd8` is thread-private | Deterministic band-local work buffers | Exact reversible patch + multisignal equivalence |
| `dsp/patched/ft8_decodevar.f90` | `wsjtx-3.0.2/lib/ft8var/ft8_decodevar.f90` | Per-thread cycle scratch and deterministic synchronized A8 owner | Remove cycle/A8 races | Exact reversible patch + team/A8/cycle tests |
| `dsp/patched/ft8_downsamplevar.f90` | `wsjtx-3.0.2/lib/ft8var/ft8_downsamplevar.f90` | Saved `cxx` cache is thread-private | Prevent cross-band cache overwrite | Exact reversible patch + parallel stress |
| `dsp/patched/ft8apsetvar.f90` | `wsjtx-3.0.2/lib/ft8var/ft8apsetvar.f90` | Clear and rebuild AP masks per request | Prevent request-context leakage | Exact reversible patch + context-switch test |

Include upstream baseline digest `a7a562c5cbcf81442d9f8b77ebf7777c1aee4a86b8e0b32c2bcdac588d4305c4`.

- [ ] **Step 5: Record I8/I9 and architecture evidence**

Update SDD §9.1/§9.2 and §11.1 with protocol version 1, bounded JSON 64 KiB control frames, parent-owned shared memory sizes, generation/request/slot matching, result capacity 256, global binding lock and fail-closed manual monitor-only restart.

Replace the I8 and I9 rows in SDD §13.5 with resolved rows:

```markdown
| I8 | Resolved at M1: protocol version 1; bounded JSON control frames; parent-owned 360,000-byte RX and 2,426,880-byte TX shared-memory segments; result capacity 256 | ABI/protocol/Worker tests |
| I9 | Resolved at M1 for this host: profile 3 safe cutoff 14.75 s; Auto thread count and full profile/thread histogram recorded in local `artifacts/dsp-benchmark.json` | M1 benchmark; remeasure on each supported CPU |
```

Insert the measured Auto thread number and p99/worst values from the artifact in prose immediately below I9. Do not invent them before running Task 9.

- [ ] **Step 6: Bump and record the implemented milestone**

Bump the SDD Quick Facts version from V1.0 to V1.1 and add `V1.1 — 2026-08-01 — M1 FT8 DSP Worker` to chapter 14, listing the ABI, standard/Improved synthetic result, Worker isolation, I8 protocol resolution, measured I9 outcome, patch copies and exact test/benchmark environment. If Task 9 is no-go, do not make this version claim; instead record an unreleased M1 investigation entry without closing I9.

This bump belongs only to Task 10 after Task 9 evidence exists. Earlier Task 5
quality fixes stay under `Unreleased` and must keep Quick Facts at V1.0.

- [ ] **Step 7: Run the full M1 gate**

Run:

```bash
cmake -S dsp -B dsp/build -DCMAKE_Fortran_COMPILER=gfortran-mp-13 -DCMAKE_BUILD_TYPE=Release
cmake --build dsp/build -j
venv/bin/python -m pytest tests/ -q
venv/bin/python scripts/benchmark_dsp.py --output artifacts/dsp-benchmark.json --iterations 10 --profiles 0,1,2,3,4 --threads 1,2,3,4,5,6,7,8,9,10,11,12 --require-safe
python3 .agents/skills/sdd-guardian/harness/sdd_context.py trace docs/superpowers/plans/2026-08-01-ft8-dsp-worker.md
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check dsp server/core tests scripts SDD AGENTS.md
```

Expected: native build succeeds; all tests PASS; benchmark exits 0 with `profile3_safe=true`; trace cites SC1/SC3, the named NFR/AD/risk/open issues; guardian prints `clean` with no block violations.

- [ ] **Step 8: Inspect repository integrity and commit**

Run `git status --short` and confirm no path under `wsjtx-3.0.2/` is modified and `artifacts/dsp-benchmark.json` remains ignored. Then, only with explicit user authorization for Git mutations:

```bash
git add AGENTS.md dsp/README.md tests/README.md tests/test_sdd_harness.py SDD/README.md SDD/09-architecture-overview.md SDD/11-component-model.md SDD/13-feasibility-assessment.md SDD/14-version-history.md
git commit -m "docs: record M1 DSP worker architecture"
```

## Execution constraints and stop conditions

- Commit steps are part of the task boundaries required by this plan, but this repository's `sdd-guardian` rule wins: execute `git add`/`git commit` only after the user explicitly authorizes Git mutations. Without that authorization, leave verified working-tree changes uncommitted and report the suggested commit.
- Stop M1 immediately if the vendor digest changes, the Improved source cannot build without the seven registered exact reversible adaptations, standard or any Improved profile cannot decode the deterministic clean fixture, Profile 3 misses 14.75 seconds for every thread count, or a fault can escape the Worker and terminate the parent test process.
- Do not respond to a benchmark miss by changing Profile 3, the cutoff, sample rates, result capacity, or process ownership. Report the measured no-go evidence and return to design review.
- Do not begin Phase 2 until Task 10 is green and I8/I9 evidence is in the SDD.
