"""Small pure-Python curve fitting / interpolation helpers.

Deliberately dependency-free so the bot installs with nothing but the stdlib.
"""

from __future__ import annotations

import math
from bisect import bisect_left


class LogLogCurve:
    """Monotone piecewise-linear interpolation through anchors in log-log space.

    Used for ADP consumption curves, where the relationship between positional
    rank and overall pick number is close to a power law but bends at the tails.
    Beyond the last anchor it extrapolates with the final segment's slope.
    """

    def __init__(self, anchors: list[tuple[float, float]]) -> None:
        pts = sorted((x, y) for x, y in anchors if x > 0 and y > 0)
        self.xs = [math.log(x) for x, _ in pts]
        self.ys = [math.log(y) for _, y in pts]
        if len(pts) < 2:
            raise ValueError("need >= 2 anchors")

    def __call__(self, x: float) -> float:
        if x <= 0:
            x = 0.5
        lx = math.log(x)
        i = bisect_left(self.xs, lx)
        if i == 0:
            i = 1
        elif i >= len(self.xs):
            i = len(self.xs) - 1
        x0, x1 = self.xs[i - 1], self.xs[i]
        y0, y1 = self.ys[i - 1], self.ys[i]
        t = (lx - x0) / (x1 - x0) if x1 != x0 else 0.0
        return math.exp(y0 + t * (y1 - y0))

    def scaled(self, factor: float) -> "LogLogCurve":
        return LogLogCurve([(math.exp(x), math.exp(y) * factor)
                            for x, y in zip(self.xs, self.ys)])


def ols(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """Simple linear regression. Returns (intercept, slope, r_squared)."""
    n = len(xs)
    if n < 2:
        return (ys[0] if ys else 0.0), 0.0, 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx if sxx else 0.0
    intercept = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
    r2 = 1 - ss_res / ss_tot if ss_tot else 0.0
    return intercept, slope, r2


class PowerLawFit:
    """Fit  ppg(rank) = A * (rank + r0)^(-b) + C  to observed (rank, ppg) pairs.

    The rank offset r0 matters: real positional value curves are noticeably
    *flat* across the top few players and only then start decaying, which an
    unshifted power law cannot represent (it blows up at rank 1 - fitting the
    guide's RB table without r0 predicts 37 PPG for RB1 against an observed
    24.8).  r0 and the floor C are chosen by grid search; given both, A and b
    fall out of an OLS fit in log-log space.
    """

    def __init__(self, points: list[tuple[int, float]]) -> None:
        pts = [(r, float(p)) for r, p in points if r > 0 and p > 0]
        pts.sort()
        self.points = pts
        self.A, self.b, self.C, self.r0, self.r2 = self._fit(pts)

    @staticmethod
    def _score(pts, A, b, C, r0) -> float:
        obs = [p for _, p in pts]
        pred = [A * (r + r0) ** (-b) + C for r, _ in pts]
        mo = sum(obs) / len(obs)
        ss_res = sum((o - q) ** 2 for o, q in zip(obs, pred))
        ss_tot = sum((o - mo) ** 2 for o in obs)
        return 1 - ss_res / ss_tot if ss_tot else 0.0

    @classmethod
    def _fit(cls, pts):
        lo = min(p for _, p in pts)
        best = (1.0, 0.5, 0.0, 0.0, -1e9)
        c_grid = [lo * 0.95 * i / 16 for i in range(17)]
        r0_grid = [i * 0.5 for i in range(29)]           # 0 .. 14
        for _ in range(3):
            for r0 in r0_grid:
                for C in c_grid:
                    usable = [(r, p - C) for r, p in pts if p - C > 1e-6]
                    if len(usable) < 5:
                        continue
                    xs = [math.log(r + r0) for r, _ in usable]
                    ys = [math.log(v) for _, v in usable]
                    a0, slope, _ = ols(xs, ys)
                    A, b = math.exp(a0), -slope
                    if b < 0:
                        continue
                    r2 = cls._score(pts, A, b, C, r0)
                    if r2 > best[4]:
                        best = (A, b, C, r0, r2)
            # refine both grids around the incumbent
            _, _, cb, rb, _ = best
            cs = (c_grid[1] - c_grid[0]) if len(c_grid) > 1 else 0.1
            rs = (r0_grid[1] - r0_grid[0]) if len(r0_grid) > 1 else 0.25
            c_grid = [max(0.0, cb + cs * (i - 4) / 4) for i in range(9)]
            r0_grid = [max(0.0, rb + rs * (i - 4) / 4) for i in range(9)]
        return best

    def __call__(self, rank: float) -> float:
        r = max(0.5, float(rank))
        return self.A * (r + self.r0) ** (-self.b) + self.C

    def __repr__(self) -> str:
        return (f"PowerLawFit(A={self.A:.2f}, b={self.b:.3f}, C={self.C:.2f}, "
                f"r0={self.r0:.2f}, r2={self.r2:.3f}, n={len(self.points)})")
