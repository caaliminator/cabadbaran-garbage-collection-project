"""
Run every phase's checks.

    python tests/run_all.py

Each file is a standalone script rather than a pytest suite, so it runs with
no test dependency at all -- in keeping with the project's stack constraint.
Phases 0 and 2-8 build their own throwaway data directory and clean it up;
phase 1 exercises the live store and reissues the demo passwords as a side
effect, which is why it prints them.
"""

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

def main() -> int:
    passed = failed = 0
    for phase in range(9):
        script = HERE / f"phase{phase}_check.py"
        if not script.exists():
            continue
        result = subprocess.run([sys.executable, str(script)],
                                capture_output=True, text=True)
        line = next((l for l in result.stdout.splitlines()
                     if "checks passed" in l), "no result")
        status = "OK  " if result.returncode == 0 else "FAIL"
        print(f"  {status} phase {phase}: {line.strip()}")
        if result.returncode == 0:
            passed += 1
        else:
            failed += 1
            for l in result.stdout.splitlines():
                if l.strip().startswith("FAIL"):
                    print(f"        {l.strip()}")
    print(f"\n{passed} phase suite(s) passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
