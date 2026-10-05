"""How confident the Metrics agent is, from the *quality* of its evidence.

Confidence is deterministic and documented here; it is not a fixed band per outcome. For an
anomaly it combines four per-metric inputs, each in 0..1, with a correlation adjustment:

    core       = 0.10 + 0.35 * strength + 0.20 * persistence + 0.25 * quality
    proximity  = 0.35 + 0.65 * closeness
    confidence = clamp(core * proximity + corroboration, 0, 0.97)

* strength    - how far past its threshold the anomaly is (detector output, 0..1).
* persistence - how long it stayed elevated: 2 consecutive samples = 0.33, 4 or more = 1.0.
* quality     - data quality from the sanity step: sample coverage of both windows, less any
                dropped non-finite values.
* closeness   - 1 / (1 + gap / 120s), where gap is the time between the last elevated sample
                and the failure: 1.0 at the failure, 0.5 two minutes before, 0.33 four before.
                It scales the whole score, so a huge spike long before the failure cannot
                outrank a moderate one at the failure.
* corroboration - set by `correlation.py`: +0.10 for each other metric that supports the
                same cause (max +0.15), -0.05 for each relevant metric that stayed normal
                (max -0.10).

A confident-looking anomaly therefore needs strong, persistent, well-measured evidence close
to the failure. With no anomaly, confidence is only `NORMAL_MAX` scaled by data quality: we
learned the metrics look normal, not what went wrong.
"""

from dataclasses import asdict, dataclass

from .anomaly import Detection

#: Samples of continuous elevation that count as fully persistent.
FULL_PERSISTENCE_RUN = 4
#: Seconds at which closeness to the failure halves.
PROXIMITY_HALF_LIFE_SECONDS = 120.0
#: Never claim certainty from metrics alone.
CONFIDENCE_CEILING = 0.97
#: Confidence when metrics are normal and well measured.
NORMAL_MAX = 0.2

_CORROBORATION_STEP = 0.10
_CORROBORATION_CAP = 0.15
_CONTRADICTION_STEP = 0.05
_CONTRADICTION_CAP = 0.10


@dataclass(frozen=True)
class ConfidenceInputs:
    strength: float
    persistence: float
    quality: float
    closeness: float

    def as_dict(self) -> dict[str, float]:
        return {key: round(value, 4) for key, value in asdict(self).items()}


def persistence(longest_run: int) -> float:
    return min(1.0, max(0.0, (longest_run - 1) / (FULL_PERSISTENCE_RUN - 1)))


def closeness(gap_seconds: float | None) -> float:
    if gap_seconds is None:
        return 0.0
    return 1 / (1 + max(gap_seconds, 0.0) / PROXIMITY_HALF_LIFE_SECONDS)


def inputs_from(detection: Detection, quality: float) -> ConfidenceInputs:
    return ConfidenceInputs(
        strength=detection.strength,
        persistence=persistence(detection.longest_run),
        quality=min(1.0, max(0.0, quality)),
        closeness=closeness(detection.gap_to_failure_seconds),
    )


def base_confidence(inputs: ConfidenceInputs) -> float:
    core = 0.10 + 0.35 * inputs.strength + 0.20 * inputs.persistence + 0.25 * inputs.quality
    return core * (0.35 + 0.65 * inputs.closeness)


def corroboration_adjustment(corroborating: int, contradicting: int) -> float:
    return min(_CORROBORATION_CAP, _CORROBORATION_STEP * corroborating) - min(
        _CONTRADICTION_CAP, _CONTRADICTION_STEP * contradicting
    )


def final_confidence(inputs: ConfidenceInputs, corroborating: int = 0, contradicting: int = 0) -> float:
    value = base_confidence(inputs) + corroboration_adjustment(corroborating, contradicting)
    return round(min(CONFIDENCE_CEILING, max(0.0, value)), 4)


def normal_confidence(qualities: list[float]) -> float:
    """Confidence when metrics were usable but showed nothing: scaled by how well they were measured."""
    if not qualities:
        return 0.0
    return round(NORMAL_MAX * sum(qualities) / len(qualities), 4)
