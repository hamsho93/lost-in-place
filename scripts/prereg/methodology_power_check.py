"""Operating characteristics of the freeze-v3 decision rule (design §8), by exact enumeration.

Tango's paired score interval is found by dense grid inversion, and P(confirm) and P(refute) come from
enumerating the multinomial (n10, n01), so the numbers carry no simulation error.

    uv run python scripts/prereg/methodology_power_check.py                # n = 100, 120, 160
    uv run python scripts/prereg/methodology_power_check.py 60 120 160
    uv run python scripts/prereg/methodology_power_check.py --bounds       # the §8 0-vs-0 table

`oc(n, p10, p01, level)` is the function the A-3 and A-3b test-retest check (check 5) uses: refute power
under a null with r = r_tr must be at least 60% at the wave's N and 98.75%.
"""

from __future__ import annotations

import argparse
import math
from functools import cache
from statistics import NormalDist

import numpy as np
import numpy.typing as npt

DELTA = 0.10
GRID = np.round(np.arange(-0.9999, 0.99995, 0.0001), 4)

LEVELS: dict[str, float] = {
    "95%": 0.95,
    "98.75%": 1 - 0.05 / 4,
    "99.58% (m = 12)": 1 - 0.05 / 12,
    "99.69% (m = 16)": 1 - 0.05 / 16,
    "99.75% (m = 20)": 1 - 0.05 / 20,
}

SCENARIOS: list[tuple[str, float, float]] = [
    ("confirm floor +10 (= delta)", 0.10, 0.0),
    ("confirm floor +15", 0.15, 0.0),
    ("confirm floor +20", 0.20, 0.0),
    ("confirm base10 +20 r2", 0.22, 0.02),
    ("confirm base30 +20 r5", 0.25, 0.05),
    ("confirm +15 r5", 0.20, 0.05),
    ("confirm +15 r10", 0.25, 0.10),
    ("refute 0v0", 0.0, 0.0),
    ("refute floor +3", 0.03, 0.0),
    ("refute null r3", 0.03, 0.03),
    ("refute null r5", 0.05, 0.05),
    ("refute null r10", 0.10, 0.10),
    ("size: confirm|null r5", 0.05, 0.05),
    ("size: refute|true=+10 r0", 0.10, 0.0),
    ("size: refute|true=+10 r5", 0.15, 0.05),
    ("size: refute|true=+10 r10", 0.20, 0.10),
]


def z_of(level: float) -> float:
    return NormalDist().inv_cdf(1 - (1 - level) / 2)


def tango_z(n10: int, n01: int, n: int, d0: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
    a = 2 * n
    b = -n10 - n01 + (2 * n - n10 + n01) * d0
    c = -n01 * d0 * (1 - d0)
    p01 = (np.sqrt(np.maximum(b * b - 4 * a * c, 0.0)) - b) / (2 * a)
    var = n * (2 * p01 + d0 * (1 - d0))
    num = n10 - n01 - n * d0
    with np.errstate(divide="ignore", invalid="ignore"):
        zz = num / np.sqrt(var)
    out: npt.NDArray[np.float64] = np.where(var <= 0, np.where(num == 0, 0.0, np.sign(num) * np.inf), zz)
    return out


@cache
def interval(n10: int, n01: int, n: int, z: float) -> tuple[float, float, float]:
    inside = GRID[np.abs(tango_z(n10, n01, n, GRID)) < z]
    return float(inside.min()), (n10 - n01) / n, float(inside.max())


def verdict(n10: int, n01: int, n: int, z: float, d: float = DELTA) -> str:
    lo, est, hi = interval(n10, n01, n, z)
    if lo > 0 and est >= d - 1e-12:
        return "C"
    if hi < d:
        return "R"
    return "I"


def _log(p: float, k: int) -> float:
    if k == 0:
        return 0.0
    return -math.inf if p <= 0 else k * math.log(p)


def pmf(n: int, a: int, b: int, p10: float, p01: float) -> float:
    rest = n - a - b
    coef = math.lgamma(n + 1) - math.lgamma(a + 1) - math.lgamma(b + 1) - math.lgamma(rest + 1)
    return math.exp(coef + _log(p10, a) + _log(p01, b) + _log(1 - p10 - p01, rest))


def oc(
    n: int, p10: float, p01: float, level: float, d: float = DELTA, tail: float = 1e-7
) -> dict[str, float]:
    """Exact P(confirm), P(refute) and P(inconclusive), enumerating the plausible (n10, n01)."""
    z = z_of(level)
    out = {"C": 0.0, "R": 0.0, "I": 0.0}
    hi10 = int(min(n, n * p10 + 8 * math.sqrt(n * p10 + 1) + 5))
    hi01 = int(min(n, n * p01 + 8 * math.sqrt(n * p01 + 1) + 5))
    for a in range(hi10 + 1):
        for b in range(min(hi01, n - a) + 1):
            p = pmf(n, a, b, p10, p01)
            if p >= tail:
                out[verdict(a, b, n, z, d)] += p
    return out


def bounds() -> None:
    print("level | z | n to refute 0 vs 0 | UB at 120 | UB at 160")
    for name, level in LEVELS.items():
        z = z_of(level)
        need = math.floor(9 * z * z) + 1
        ub = [100 * z * z / (n + z * z) for n in (120, 160)]
        print(f"{name} | {z:.3f} | {need} | {ub[0]:.1f} | {ub[1]:.1f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ns", nargs="*", type=int, default=[100, 120, 160])
    ap.add_argument("--bounds", action="store_true", help="print only the 0-vs-0 upper-bound table")
    args = ap.parse_args()
    if args.bounds:
        bounds()
        return
    for name, level in LEVELS.items():
        print(f"\n## level {name}")
        print("scenario | " + " | ".join(f"n={n}" for n in args.ns))
        for label, p10, p01 in SCENARIOS:
            cells = []
            for n in args.ns:
                r = oc(n, p10, p01, level)
                cells.append(f"C{r['C'] * 100:.1f}/R{r['R'] * 100:.1f}")
            print(label + " | " + " | ".join(cells))


if __name__ == "__main__":
    main()
