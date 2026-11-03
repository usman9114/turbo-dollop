"""Price model: log-linear ridge regression with informative priors.

    log(price) = b0 + b_line + b_age·age + b_ram·log2(RAM/8) + b_ssd·log2(SSD/256)
                 + b_condition + b_no_charger + b_ebay + b_sold + b_vb + (unknown-spec flags)

Coefficients are shrunk toward PRIOR (hand-set from the plan's market research) with strength
LAMBDA, i.e. each prior counts like a few observations. With no data the model returns the
priors; as listings, sold comps and your own sales accumulate it learns the real market.
Pure Python (no numpy) so the same file runs inside a Claude skill.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

from .features import Features

LINES = ["thinkpad_x1", "thinkpad_p", "thinkpad_t", "thinkpad_x", "thinkpad_l", "thinkpad_e", "thinkpad_other",
         "latitude_7", "latitude_5", "latitude_3", "elitebook", "probook", "macbook_pro", "macbook_air", "surface",
         "dyson_v15", "dyson_v11", "dyson_v8"]
CONDITIONS = ["new", "like_new", "worn", "defect"]        # baseline: "good" (normal use, works)

# Prior means. Baseline: generic laptop, age 0, 8 GB / 256 GB, good condition, Kleinanzeigen asking price.
# Calibrated so a T480 (2018, 8 GB) asks ≈ €185 and a T14 Gen 2 (2021, 16 GB) ≈ €320 on Kleinanzeigen.
PRIOR: dict[str, float] = {
    "intercept": math.log(539),
    "age": -0.15, "ram": 0.10, "ssd": 0.05,
    "ram_unknown": -0.03, "ssd_unknown": -0.03, "year_unknown": 0.0,
    "no_charger": -0.08, "ebay": 0.08, "sold": -0.10, "vb": -0.03,
    "line_thinkpad_x1": 0.35, "line_thinkpad_p": 0.30, "line_thinkpad_t": 0.13, "line_thinkpad_x": 0.05,
    "line_thinkpad_l": -0.10, "line_thinkpad_e": -0.20, "line_thinkpad_other": 0.0,
    "line_latitude_7": 0.15, "line_latitude_5": 0.0, "line_latitude_3": -0.25,
    "line_elitebook": 0.05, "line_probook": -0.15,
    "line_macbook_pro": 0.75, "line_macbook_air": 0.55, "line_surface": 0.20,
    "line_dyson_v15": 0.10, "line_dyson_v11": 0.10, "line_dyson_v8": 0.0,
    "cond_new": 0.35, "cond_like_new": 0.08, "cond_worn": -0.10, "cond_defect": -1.00,
}
NAMES = list(PRIOR)
LAMBDA = 4.0            # prior strength (pseudo-observations) for every coefficient …
LAMBDA_INTERCEPT = 1.0  # … except the intercept, which data may move freely
PRIOR_SIGMA = 0.25      # residual sd in log space (≈ ±25%) before data says otherwise
DEFAULT_AGE = 8.0


def vectorize(f: Features) -> list[float]:
    x = dict.fromkeys(NAMES, 0.0)
    x["intercept"] = 1.0
    if f.age is None:
        x["age"], x["year_unknown"] = DEFAULT_AGE, 1.0
    else:
        x["age"] = min(f.age, 15.0)
    if f.ram_gb:
        x["ram"] = math.log2(f.ram_gb / 8)
    else:
        x["ram_unknown"] = 1.0
    if f.ssd_gb:
        x["ssd"] = math.log2(f.ssd_gb / 256)
    else:
        x["ssd_unknown"] = 1.0
    x["no_charger"] = 1.0 if f.charger is False else 0.0
    x["ebay"] = 1.0 if f.platform == "ebay" else 0.0
    x["sold"] = 1.0 if f.price_kind == "sold" else 0.0
    x["vb"] = 1.0 if f.negotiable else 0.0
    if f.line in LINES:
        x[f"line_{f.line}"] = 1.0
    if f.condition in CONDITIONS:
        x[f"cond_{f.condition}"] = 1.0
    return [x[n] for n in NAMES]


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting (A is SPD and small)."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        m[col], m[piv] = m[piv], m[col]
        for r in range(n):
            if r != col and m[r][col]:
                fac = m[r][col] / m[col][col]
                for c in range(col, n + 1):
                    m[r][c] -= fac * m[col][c]
    return [m[i][n] / m[i][i] for i in range(n)]


def _inverse(a: list[list[float]]) -> list[list[float]]:
    n = len(a)
    cols = [_solve(a, [1.0 if i == j else 0.0 for i in range(n)]) for j in range(n)]
    return [[cols[j][i] for j in range(n)] for i in range(n)]


@dataclass
class Estimate:
    price: float          # median
    low: float            # 10th percentile
    high: float           # 90th percentile
    n_line: int           # training rows for this product line
    source: str

    def as_dict(self) -> dict:
        return {k: (round(v, 0) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


@dataclass
class PriceModel:
    coef: list[float] = field(default_factory=lambda: [PRIOR[n] for n in NAMES])
    sigma: float = PRIOR_SIGMA
    a_inv: list[list[float]] | None = None
    n: int = 0
    line_counts: dict[str, int] = field(default_factory=dict)

    # -- training -------------------------------------------------------------
    @classmethod
    def fit(cls, rows: list[tuple[Features, float, float]]) -> "PriceModel":
        """rows: (features, price_eur, weight). Ridge toward PRIOR: β = (XᵀWX + Λ)⁻¹ (XᵀWy + Λβ₀)."""
        k = len(NAMES)
        lam = [LAMBDA_INTERCEPT if n == "intercept" else LAMBDA for n in NAMES]
        a = [[0.0] * k for _ in range(k)]
        b = [0.0] * k
        for i in range(k):
            a[i][i] = lam[i]
            b[i] = lam[i] * PRIOR[NAMES[i]]
        line_counts: dict[str, int] = {}
        clean = []
        for feats, price, w in rows:
            if not price or price < 5:
                continue
            x, y = vectorize(feats), math.log(price)
            clean.append((x, y, w))
            line_counts[feats.line] = line_counts.get(feats.line, 0) + 1
            for i in range(k):
                if x[i]:
                    b[i] += w * x[i] * y
                    for j in range(k):
                        if x[j]:
                            a[i][j] += w * x[i] * x[j]
        coef = _solve(a, b)
        # Residual sd, shrunk toward the prior sd (10 pseudo-observations).
        sse = sum(w * (y - sum(c * xi for c, xi in zip(coef, x))) ** 2 for x, y, w in clean)
        wsum = sum(w for *_, w in clean)
        sigma = math.sqrt((sse + 10 * PRIOR_SIGMA ** 2) / (wsum + 10))
        return cls(coef=coef, sigma=sigma, a_inv=_inverse(a), n=len(clean), line_counts=line_counts)

    # -- prediction -----------------------------------------------------------
    def predict(self, f: Features) -> Estimate:
        x = vectorize(f)
        mu = sum(c * xi for c, xi in zip(self.coef, x))
        var = self.sigma ** 2
        if self.a_inv is not None:  # parameter uncertainty: larger for rarely-seen lines
            var += self.sigma ** 2 * sum(x[i] * self.a_inv[i][j] * x[j] for i in range(len(x)) for j in range(len(x)))
        else:
            var += (self.sigma ** 2) * 0.5
        if f.age is None:
            var += 0.35 ** 2      # unknown generation: much wider range
        sd = math.sqrt(var)
        n_line = self.line_counts.get(f.line, 0)
        src = f"model (n={self.n}, {n_line} {f.line})" if self.n else "model priors (no data yet)"
        return Estimate(price=math.exp(mu), low=math.exp(mu - 1.2816 * sd), high=math.exp(mu + 1.2816 * sd),
                        n_line=n_line, source=src)

    def explain(self) -> dict[str, float]:
        return {n: round(c, 3) for n, c in zip(NAMES, self.coef)}

    # -- persistence (for the Claude skill) -----------------------------------
    def to_json(self) -> str:
        return json.dumps({"names": NAMES, "coef": self.coef, "sigma": self.sigma, "n": self.n,
                           "line_counts": self.line_counts}, indent=1)

    @classmethod
    def from_json(cls, text: str) -> "PriceModel":
        d = json.loads(text)
        coef = [d["coef"][d["names"].index(n)] if n in d["names"] else PRIOR[n] for n in NAMES]
        return cls(coef=coef, sigma=d["sigma"], n=d["n"], line_counts=d.get("line_counts", {}))


def can_price(f: Features) -> bool:
    """Only price things we have a tier for and know the age of (or a known line)."""
    return f.line not in ("other",) and (f.year is not None or f.line not in ("laptop_generic", "thinkpad_other"))


def fit_from_db(db) -> PriceModel:
    """Train on everything the bot has seen. Accessories and unpriceable rows are skipped."""
    from .features import extract
    from .rules import is_accessory
    rows = []
    for title, desc, price, platform, kind, vb, cond_field, weight in db.training_rows():
        if is_accessory(title):
            continue
        feats = extract(title, desc, platform, price_kind=kind, negotiable=vb, condition_field=cond_field)
        if can_price(feats):
            rows.append((feats, price, weight))
    return PriceModel.fit(rows)
