"""Close the loop: what did we predict, and what actually happened?

Every `outcome` row carries `est_out_bytes` beside `after_bytes` and
`est_cpu_seconds` beside `cpu_seconds`, recorded for exactly this. Once enough
real jobs have run, the ratio between them is a correction factor, and the
estimator applies it to everything it predicts next.

This matters more here than in a project where the queue finishes. The planner
ranks by predicted GB per encode-hour, so a systematically wrong estimate does
not merely misreport -- it puts the wrong files first, for months. A model that
under-predicts output size by a third promotes files that were never worth the
hour, and the files that were sit behind them all year.

Three things keep the correction honest:

- **Per model, not in aggregate.** `estimate_basis` records which of the
  estimator's models produced each number -- the ladder's measured bitrate, the
  ladder's size ratio, the policy target, a stream copy. They are wrong in
  different directions and averaging them hides both.
- **Median, not mean.** One pathological file -- a concert film, a source with
  a corrupt tail -- should not move the model for everything behind it.
- **Bounded, and it says when it hits the bound.** A factor outside the sane
  range means something is wrong with the measurement rather than with the
  estimator, and quietly applying it would be worse than applying nothing.

The correction is *not* folded back into the raw estimate silently: a corrected
estimate records its basis as `<model>+cal`, so the next calibration measures
the corrected model separately and its factor converges towards 1 rather than
compounding on itself.
"""

from __future__ import annotations

import json
import statistics
import time
from dataclasses import dataclass, field

KV_KEY = "estimator_calibration"

# Below this many measurements a factor is noise, and noise applied to a
# months-long queue is worse than no correction at all.
MIN_SAMPLES = 8

# A correction outside this range is not a wrong estimator, it is a broken
# measurement -- an interrupted job, a mismatched pair of rows. Clamp, and say
# so in the report rather than silently shipping it.
MIN_FACTOR = 0.25
MAX_FACTOR = 4.0

# Only interesting once it is bigger than the noise in the underlying encodes.
WORTH_APPLYING = 0.05


@dataclass
class Calibration:
    """Correction factors the estimator multiplies its predictions by."""

    size_factors: dict[str, float] = field(default_factory=dict)
    speed_factors: dict[str, float] = field(default_factory=dict)
    size_default: float = 1.0
    speed_default: float = 1.0
    samples: int = 0
    built_at: float = 0.0

    def size_factor(self, basis: str) -> float:
        return self.size_factors.get(basis, self.size_default)

    def speed_factor(self, key: str) -> float:
        """Exact key first, then the encoder on its own, then the pooled value.

        Encode speed varies far more with resolution than with anything else --
        the first real benchmark run reported 61 fps by averaging SD into the
        1080p figure, which no 1080p file will ever see. So the specific key
        wins whenever there is one.
        """
        if key in self.speed_factors:
            return self.speed_factors[key]
        encoder = key.split(":", 1)[0]
        return self.speed_factors.get(encoder, self.speed_default)

    def to_json(self) -> str:
        return json.dumps({
            "size_factors": self.size_factors,
            "speed_factors": self.speed_factors,
            "size_default": self.size_default,
            "speed_default": self.speed_default,
            "samples": self.samples,
            "built_at": self.built_at,
        })

    @classmethod
    def from_json(cls, text: str) -> "Calibration":
        data = json.loads(text)
        return cls(
            size_factors={k: float(v) for k, v in (data.get("size_factors") or {}).items()},
            speed_factors={k: float(v) for k, v in (data.get("speed_factors") or {}).items()},
            size_default=float(data.get("size_default") or 1.0),
            speed_default=float(data.get("speed_default") or 1.0),
            samples=int(data.get("samples") or 0),
            built_at=float(data.get("built_at") or 0.0),
        )


@dataclass
class Group:
    """One model's worth of measurements, and what they say about it."""

    key: str
    kind: str            # "size" | "speed"
    ratios: list[float] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.ratios)

    @property
    def median(self) -> float:
        return statistics.median(self.ratios) if self.ratios else 1.0

    @property
    def spread(self) -> float:
        """How much the measurements disagree. A wide spread means the factor
        is a summary of two different populations, not a correction."""
        if len(self.ratios) < 2:
            return 0.0
        low, high = min(self.ratios), max(self.ratios)
        return high - low

    @property
    def factor(self) -> float:
        return max(MIN_FACTOR, min(MAX_FACTOR, self.median))

    @property
    def clamped(self) -> bool:
        return abs(self.factor - self.median) > 1e-9

    @property
    def usable(self) -> bool:
        return self.n >= MIN_SAMPLES

    @property
    def reading(self) -> str:
        """The factor in English, in the direction that matters to a queue.

        A factor above 1 means the real file came out bigger, or the encode
        took longer, than the estimate promised -- so the estimate was too
        optimistic and the file was ranked too highly.
        """
        off_by = abs(1 - self.median) * 100
        thing = "output size" if self.kind == "size" else "encode time"
        if off_by < WORTH_APPLYING * 100:
            return f"predicted {thing} is about right"
        direction = "under" if self.median > 1 else "over"
        return f"predicted {thing} is {direction} by {off_by:.0f}%"

    def describe(self) -> str:
        return f"{self.key:<28} n={self.n:<4} x{self.median:.2f}   {self.reading}"


@dataclass
class Report:
    considered: int = 0
    used: int = 0
    size: list[Group] = field(default_factory=list)
    speed: list[Group] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    calibration: Calibration | None = None

    @property
    def enough(self) -> bool:
        return self.calibration is not None

    def summary(self) -> str:
        lines = [
            f"  outcomes   {self.considered} recorded, {self.used} usable",
        ]
        if not self.size and not self.speed:
            lines.append(
                "  nothing to compare yet. Estimates are checked against real "
                "jobs, so this needs `app work` to have finished some."
            )
            return "\n".join(lines)

        lines.append("\n  output size, by the model that predicted it:")
        for group in self.size:
            mark = "  " if group.usable else "  (too few) "
            lines.append(f"    {mark}{group.describe()}")
        lines.append("\n  encode time, by encoder and resolution:")
        for group in self.speed:
            mark = "  " if group.usable else "  (too few) "
            lines.append(f"    {mark}{group.describe()}")
        for warning in self.warnings:
            lines.append(f"\n  ! {warning}")
        return "\n".join(lines)


# ------------------------------------------------------------------ measuring


def _speed_key(row) -> str:
    encoder = row["encoder"] or row["action"] or "?"
    return f"{encoder}:{row['resolution'] or '?'}"


def measure(db, *, min_samples: int = MIN_SAMPLES) -> Report:
    """Compare every recorded outcome against what was predicted for it."""
    report = Report()
    rows = db.query("SELECT * FROM outcome ORDER BY completed_at")
    report.considered = len(rows)

    size_groups: dict[str, Group] = {}
    speed_groups: dict[str, Group] = {}
    all_size: list[float] = []
    all_speed: list[float] = []

    for row in rows:
        counted = False

        est_out = row["est_out_bytes"]
        if est_out and est_out > 0 and row["after_bytes"] > 0:
            basis = row["estimate_basis"] or "unknown"
            ratio = row["after_bytes"] / est_out
            size_groups.setdefault(basis, Group(basis, "size")).ratios.append(ratio)
            all_size.append(ratio)
            counted = True

        est_cpu = row["est_cpu_seconds"]
        # A held output installed later records zero seconds -- the encode
        # happened on an earlier run and its time is already counted there.
        if est_cpu and est_cpu > 0 and (row["cpu_seconds"] or 0) > 0:
            key = _speed_key(row)
            ratio = row["cpu_seconds"] / est_cpu
            speed_groups.setdefault(key, Group(key, "speed")).ratios.append(ratio)
            all_speed.append(ratio)
            counted = True

        if counted:
            report.used += 1

    report.size = sorted(size_groups.values(), key=lambda g: -g.n)
    report.speed = sorted(speed_groups.values(), key=lambda g: -g.n)

    for group in report.size + report.speed:
        if group.usable and group.clamped:
            report.warnings.append(
                f"{group.key}: measured x{group.median:.2f}, clamped to "
                f"x{group.factor:.2f}. A correction that large is more likely a "
                f"broken measurement than a broken estimator -- look at those "
                f"outcomes before trusting it."
            )
        if group.usable and group.spread > 1.5:
            report.warnings.append(
                f"{group.key}: the measurements range over {group.spread:.1f}x, "
                f"so one factor is summarising content that does not behave "
                f"alike. The correction will be right on average and wrong on "
                f"most individual files."
            )

    usable_size = [g for g in report.size if g.n >= min_samples]
    usable_speed = [g for g in report.speed if g.n >= min_samples]
    if not usable_size and not usable_speed:
        return report

    report.calibration = Calibration(
        size_factors={g.key: g.factor for g in usable_size},
        speed_factors={g.key: g.factor for g in usable_speed},
        size_default=_clamped_median(all_size) if len(all_size) >= min_samples else 1.0,
        speed_default=_clamped_median(all_speed) if len(all_speed) >= min_samples else 1.0,
        samples=report.used,
        built_at=time.time(),
    )
    return report


def _clamped_median(values: list[float]) -> float:
    if not values:
        return 1.0
    return max(MIN_FACTOR, min(MAX_FACTOR, statistics.median(values)))


# ------------------------------------------------------------------ storage


def load(db) -> Calibration | None:
    """The calibration in force, or None if the estimator is still uncorrected."""
    raw = db.get_kv(KV_KEY)
    if not raw:
        return None
    try:
        return Calibration.from_json(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def save(db, calibration: Calibration) -> None:
    db.set_kv(KV_KEY, calibration.to_json())


def clear(db) -> None:
    db.execute("DELETE FROM kv WHERE key=?", (KV_KEY,))


def describe(calibration: Calibration | None) -> str:
    if calibration is None:
        return "no calibration; estimates are the raw model"
    parts = [f"{k} x{v:.2f}" for k, v in sorted(calibration.size_factors.items())]
    return (
        f"calibrated from {calibration.samples} outcome(s): "
        + (", ".join(parts) if parts else f"pooled x{calibration.size_default:.2f}")
    )
