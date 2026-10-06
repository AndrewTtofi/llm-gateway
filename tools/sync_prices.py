"""Propose updates to config/pricing.yaml and config/catalog.yaml from public catalogs.

    python -m tools.sync_prices            # show the diff; exit 1 if anything changed
    python -m tools.sync_prices --write    # apply prices both sources agree on, and facts

Exit codes: 0 up to date · 1 changes found · 2 bad arguments (argparse) ·
3 the catalogs couldn't be fetched · 4 internal error. Only 1 means drift.

No provider publishes prices through an API, so this reads two community catalogs:

- LiteLLM's model list (primary): keyed by the providers' own model IDs, and includes
  context windows and capability flags.
- OpenRouter's /models API (cross-check): its own IDs, prices per token.

Prices drive budgets and spend reports, so nothing changes silently:

- A price change is `agreed` when both sources give the same value. `--write` applies only
  those. `disputed` (they differ) and `unchecked` (OpenRouter has no such model) need
  `--force`, after checking the provider's pricing page.
- Facts (context window, max output, capabilities) don't affect billing; `--write` applies them.
- Remote values are validated (finite, non-negative prices; positive integer sizes) before
  they are compared, printed or written, so bad remote data can't reach the files or issues.
- Quality scores in catalog.yaml are the operator's and are never changed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
import yaml

LITELLM_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)
OPENROUTER_URL = "https://openrouter.ai/api/v1/models"
RATE_FIELDS = ("input", "output", "cached_input", "cache_write", "cache_write_1h")
PRICE_FIELDS = (*RATE_FIELDS, "tiers")  # everything that bills someone
LITELLM_KEYS = {
    "input": "input_cost_per_token",
    "output": "output_cost_per_token",
    "cached_input": "cache_read_input_token_cost",
    "cache_write": "cache_creation_input_token_cost",
    "cache_write_1h": "cache_creation_input_token_cost_above_1hr",
}
OPENROUTER_KEYS = {
    "input": "prompt",
    "output": "completion",
    "cached_input": "input_cache_read",
    "cache_write": "input_cache_write",
    "cache_write_1h": "input_cache_write_1h",
}
_LITELLM_TIER = re.compile(r"^input_cost_per_token_above_(\d+)k_tokens$")
FACT_FIELDS = ("context_window", "max_output_tokens", "capabilities")
CAPABILITIES = {  # LiteLLM flag → catalog capability
    "supports_function_calling": "tools",
    "supports_vision": "vision",
    "supports_reasoning": "reasoning",
    "supports_response_schema": "json_schema",
}
SKIP_TYPES = {"fake"}  # never priced from public sources
TOLERANCE = 0.005  # relative difference treated as equal (rounding in the sources)

EXIT_OK, EXIT_DRIFT, EXIT_FETCH, EXIT_INTERNAL = 0, 1, 3, 4
Status = Literal["agreed", "disputed", "unchecked"]


@dataclass
class Change:
    target: str
    field: str
    current: Any
    proposed: Any
    check: Any = None  # the cross-check source's value, if it has one
    status: Status = "agreed"

    @property
    def is_price(self) -> bool:
        return self.field in PRICE_FIELDS

    @property
    def safe(self) -> bool:
        """Applied by --write without --force."""
        return not self.is_price or self.status == "agreed"

    def row(self) -> str:
        flag = {
            "agreed": "",
            "disputed": "  ⚠ sources disagree",
            "unchecked": "  ⚠ unchecked: not on OpenRouter",
        }[self.status if self.is_price else "agreed"]
        check = f"  (openrouter: {self.check})" if self.check is not None else ""
        change = f"{self.current!s:>12} → {self.proposed!s:<12}"
        return f"{self.target:42} {self.field:18} {change}{check}{flag}"


# --- validation of remote values ---------------------------------------------


def per_million(per_token: Any) -> float | None:
    """USD per token → per 1M tokens. Anything that isn't a finite, non-negative number
    (strings, NaN, inf, negatives) is treated as unknown."""
    if isinstance(per_token, bool):
        return None
    try:
        value = float(per_token)
    except TypeError, ValueError:
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return round(value * 1_000_000, 6)


def positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _status(new: float, other: float | None) -> Status:
    return "unchecked" if other is None else "agreed" if same(new, other) else "disputed"


def litellm_tiers(src: dict[str, Any]) -> list[dict[str, Any]]:
    """Long-context tiers from LiteLLM's `*_above_<N>k_tokens` prices (ADR 0015)."""
    tiers = []
    for key in src:
        if not (m := _LITELLM_TIER.match(key)):
            continue
        suffix = f"_above_{m.group(1)}k_tokens"
        tier: dict[str, Any] = {"above_prompt_tokens": int(m.group(1)) * 1000}
        for f in ("input", "output", "cached_input", "cache_write"):
            if (v := per_million(src.get(LITELLM_KEYS[f] + suffix))) is not None:
                tier[f] = v
        if "input" in tier and "output" in tier:
            tiers.append(tier)
    return sorted(tiers, key=lambda t: t["above_prompt_tokens"])


def openrouter_tiers(prices: dict[str, Any]) -> list[dict[str, Any]] | None:
    """OpenRouter's `overrides` (min_prompt_tokens → prices); None when it lists none."""
    overrides = prices.get("overrides")
    if not isinstance(overrides, list) or not overrides:
        return None
    tiers = []
    for o in overrides:
        if not isinstance(o, dict) or (n := positive_int(o.get("min_prompt_tokens"))) is None:
            continue
        tier: dict[str, Any] = {"above_prompt_tokens": n}
        for f in ("input", "output", "cached_input", "cache_write"):
            if (v := per_million(o.get(OPENROUTER_KEYS[f]))) is not None:
                tier[f] = v
        tiers.append(tier)
    return sorted(tiers, key=lambda t: t["above_prompt_tokens"]) or None


def same_tiers(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> bool:
    """Same thresholds, and every price both sides list agrees."""
    if [t.get("above_prompt_tokens") for t in a] != [t.get("above_prompt_tokens") for t in b]:
        return False
    for x, y in zip(a, b, strict=True):
        for f in ("input", "output", "cached_input", "cache_write"):
            if f in x and f in y and not same(float(x[f]), float(y[f])):
                return False
            if (f in x) != (f in y) and f in ("input", "output"):
                return False
    return True


def same(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is b
    return abs(a - b) <= TOLERANCE * max(abs(a), abs(b), 1e-12)


def openrouter_id(target: str) -> str:
    """Best guess at OpenRouter's ID: claude-haiku-4-5-20251001 → claude-haiku-4.5."""
    provider, _, model = target.partition("/")
    model = re.sub(r"-\d{8}$", "", model)
    model = re.sub(r"-(\d+)-(\d+)$", r"-\1.\2", model)
    return f"{provider}/{model}"


def targets(models_yaml: dict[str, Any], pricing: dict[str, Any]) -> list[str]:
    """Every priced target and every target in an alias chain, minus test providers."""
    providers = models_yaml.get("providers", {})
    found = set(pricing.get("models", {}))
    for alias in models_yaml.get("aliases", {}).values():
        found.update(alias.get("chain", []))
    keep = []
    for t in sorted(found):
        cfg = providers.get(t.partition("/")[0], {})
        if cfg.get("type") in SKIP_TYPES or cfg.get("dev_only"):
            continue
        keep.append(t)
    return keep


def source_for(
    target: str, litellm: dict[str, Any], entry: dict[str, Any]
) -> dict[str, Any] | None:
    """The LiteLLM entry for a target. An explicit `litellm_id` wins, then the
    provider-prefixed key; a bare model ID only counts if LiteLLM files it under the same
    provider (the same model name can be another provider's listing, e.g. Vertex AI)."""
    provider, _, model = target.partition("/")
    if (explicit := entry.get("litellm_id")) and isinstance(src := litellm.get(explicit), dict):
        return src
    if isinstance(src := litellm.get(target), dict):
        return src
    src = litellm.get(model)
    if isinstance(src, dict) and src.get("litellm_provider") in (provider, None):
        return src
    return None


def propose(
    target: str,
    litellm: dict[str, Any],
    openrouter: dict[str, Any],
    price: dict[str, Any],
    entry: dict[str, Any],
) -> list[Change]:
    src = source_for(target, litellm, entry)
    if src is None:
        return []
    check = openrouter.get(entry.get("openrouter_id") or openrouter_id(target))
    check_prices = check.get("pricing") if isinstance(check, dict) else None
    check_prices = check_prices if isinstance(check_prices, dict) else {}
    proposed = {f: per_million(src.get(k)) for f, k in LITELLM_KEYS.items()}
    checked = {f: per_million(check_prices.get(k)) for f, k in OPENROUTER_KEYS.items()}
    out: list[Change] = []
    for f in RATE_FIELDS:
        new, cur, other = proposed[f], price.get(f), checked[f]
        if new is None or same(new, cur):
            continue
        out.append(Change(target, f, cur, new, other, _status(new, other)))

    tiers, check_tiers = litellm_tiers(src), openrouter_tiers(check_prices)
    if tiers and not same_tiers(tiers, price.get("tiers") or []):
        status: Status = (
            "unchecked"
            if check_tiers is None
            else "agreed"
            if same_tiers(tiers, check_tiers)
            else "disputed"
        )
        out.append(Change(target, "tiers", price.get("tiers"), tiers, check_tiers, status))

    facts: dict[str, Any] = {
        "context_window": positive_int(src.get("max_input_tokens")),
        "max_output_tokens": positive_int(src.get("max_output_tokens")),
        "capabilities": sorted(c for flag, c in CAPABILITIES.items() if src.get(flag) is True),
    }
    pinned = entry.get("pinned") or []  # facts the operator checked by hand: never proposed
    for f, new in facts.items():
        if f in pinned:
            continue
        current = entry.get(f)
        if f == "capabilities" and isinstance(current, list):
            current = sorted(current)  # order isn't a change
        if new in (None, []) or new == current:
            continue
        out.append(Change(target, f, entry.get(f), new))
    return out


# --- writing -----------------------------------------------------------------

_PLAIN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./:-]*")


def _price(v: float) -> str:
    """At least two decimals, like the hand-written file: 10.00, 0.10, 0.125."""
    whole, _, frac = f"{v:.6f}".rstrip("0").partition(".")
    return f"{whole}.{frac.ljust(2, '0')}"


def _scalar(v: Any, prices: bool, key: str = "") -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int | float):
        if not math.isfinite(v):
            raise ValueError(f"refusing to write non-finite number {v!r}")
        # Only rates look like prices; thresholds, multipliers and sizes stay as they are.
        return _price(float(v)) if prices and key in RATE_FIELDS else str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_scalar(x, prices, key) for x in v) + "]"
    if isinstance(v, dict):
        return _flow(v, prices)
    s = str(v)
    # Plain only when it can't be read as anything else; otherwise a quoted YAML string.
    return s if _PLAIN.fullmatch(s) and yaml.safe_load(s) == s else json.dumps(s)


def _flow(values: dict[str, Any], prices: bool = False) -> str:
    return "{ " + ", ".join(f"{k}: {_scalar(v, prices, k)}" for k, v in values.items()) + " }"


def _patch_models(text: str, updates: dict[str, dict[str, Any]], prices: bool, note: str) -> str:
    """Replace `  target: { … }` lines under `models:` in place, keeping indentation, the
    alignment after the colon and any trailing comment. Targets without a line are
    inserted at the end of the `models:` block."""
    lines = text.splitlines()
    for target, values in updates.items():
        pattern = re.compile(
            rf"^(\s+)({re.escape(target)}|\"{re.escape(target)}\"):(\s*)\{{[^}}]*\}}(.*)$"
        )
        for i, line in enumerate(lines):
            if m := pattern.match(line):
                lines[i] = (
                    f"{m.group(1)}{m.group(2)}:{m.group(3)}{_flow(values, prices)}{m.group(4)}"
                )
                break
        else:
            # After the last indented line of the block; the block ends at the first
            # non-blank line in column 0 (the next key, or a comment block).
            start = next(i for i, line in enumerate(lines) if line.rstrip() == "models:")
            end = start + 1
            for i in range(start + 1, len(lines)):
                if lines[i].startswith((" ", "\t")):
                    end = i + 1
                elif lines[i].strip():
                    break
            lines.insert(end, f"  {target}: {_flow(values, prices)}  # {note}")
    return "\n".join(lines) + "\n"


def write_pricing(path: Path, changes: list[Change], today: str) -> bool:
    """Apply price changes. Returns whether anything was written."""
    updates_raw = [c for c in changes if c.is_price]
    if not updates_raw:
        return False
    text = path.read_text()
    current = (yaml.safe_load(text) or {}).get("models") or {}
    updates: dict[str, dict[str, Any]] = {}
    for c in updates_raw:
        updates.setdefault(c.target, dict(current.get(c.target) or {}))[c.field] = c.proposed
    ordered = {
        t: {f: v[f] for f in PRICE_FIELDS if v.get(f) is not None}
        | {k: x for k, x in v.items() if k not in PRICE_FIELDS}
        for t, v in updates.items()
    }
    out = _patch_models(text, ordered, prices=True, note=f"added by sync_prices {today}")
    out = re.sub(r"(?m)^checked: .*$", f"checked: {today}", out, count=1)
    path.write_text(out)
    return True


CATALOG_HEADER = """\
# Model facts for GET /v1/catalog (and, later, policy routing). ADR 0011.
# context_window / max_output_tokens / capabilities: synced by `make prices` from public
# catalogs — review the diff before committing. quality: YOUR score, 1 (basic) to 5
# (frontier), for your own use cases; the sync never changes it.
# Optional per model: litellm_id / openrouter_id when the source uses a different ID;
# pinned: [field, …] for facts you checked by hand, which the sync then leaves alone.

checked: {today}
models:
"""


def write_catalog(path: Path, changes: list[Change], today: str) -> bool:
    """Apply fact changes in place (comments and other keys are kept). Returns whether
    anything was written."""
    facts = [c for c in changes if not c.is_price]
    if not facts:
        return False
    text = path.read_text() if path.exists() else CATALOG_HEADER.format(today=today)
    current = (yaml.safe_load(text) or {}).get("models") or {}
    updates: dict[str, dict[str, Any]] = {}
    for c in facts:
        updates.setdefault(c.target, dict(current.get(c.target) or {}))[c.field] = c.proposed
    order = (*FACT_FIELDS, "quality")
    ordered = {
        t: {k: v[k] for k in order if k in v} | {k: x for k, x in v.items() if k not in order}
        for t, v in updates.items()
    }
    out = _patch_models(text, ordered, prices=False, note=f"added by sync_prices {today}")
    out = re.sub(r"(?m)^checked: .*$", f"checked: {today}", out, count=1)
    path.write_text(out)
    return True


# --- CLI ---------------------------------------------------------------------


def fetch() -> tuple[dict[str, Any], dict[str, Any]]:
    with httpx.Client(timeout=60, follow_redirects=True) as http:
        litellm = http.get(LITELLM_URL).raise_for_status().json()
        data = http.get(OPENROUTER_URL).raise_for_status().json()["data"]
    if not isinstance(litellm, dict) or not isinstance(data, list):
        raise ValueError("unexpected catalog format")
    return litellm, {
        m["id"]: m for m in data if isinstance(m, dict) and isinstance(m.get("id"), str)
    }


def diff(
    config_dir: Path, litellm: dict[str, Any], openrouter: dict[str, Any]
) -> tuple[list[Change], list[str]]:
    models_yaml = yaml.safe_load((config_dir / "models.yaml").read_text())
    pricing = yaml.safe_load((config_dir / "pricing.yaml").read_text())
    catalog_path = config_dir / "catalog.yaml"
    catalog = (yaml.safe_load(catalog_path.read_text()) if catalog_path.exists() else None) or {}
    changes: list[Change] = []
    missing: list[str] = []
    for t in targets(models_yaml, pricing):
        price = (pricing.get("models") or {}).get(t) or {}
        entry = (catalog.get("models") or {}).get(t) or {}
        if source_for(t, litellm, entry) is None:
            missing.append(t)
            continue
        changes += propose(t, litellm, openrouter, price, entry)
    return changes, missing


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--config-dir", type=Path, default=Path("config"))
    p.add_argument("--write", action="store_true", help="apply agreed prices and all facts")
    p.add_argument(
        "--force", action="store_true", help="with --write: also disputed/unchecked prices"
    )
    p.add_argument("--json", action="store_true", help="machine-readable output")
    args = p.parse_args(argv)

    try:
        litellm, openrouter = fetch()
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        print(f"could not fetch the price catalogs: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FETCH  # not drift: nothing was compared
    try:
        changes, missing = diff(args.config_dir, litellm, openrouter)
        if args.json:
            rows = [c.__dict__ for c in changes]
            print(json.dumps({"changes": rows, "no_source": missing}, indent=2))
        else:
            for c in changes:
                print(c.row())
            if missing:
                print(f"\nno public source (kept as is): {', '.join(missing)}")
            if not changes:
                print("pricing and catalog are up to date")
        if args.write and changes:
            apply = [c for c in changes if c.safe or args.force]
            today = dt.date.today().isoformat()
            wrote = write_pricing(args.config_dir / "pricing.yaml", apply, today)
            wrote |= write_catalog(args.config_dir / "catalog.yaml", apply, today)
            held = len(changes) - len(apply)
            print(
                f"\nwrote {len(apply)} change(s)"
                + (f"; {held} held back (need --force)" if held else "")
            )
            if wrote:
                print("review with `git diff config/`, then `make reload`")
    except Exception:
        traceback.print_exc()
        return EXIT_INTERNAL
    return EXIT_DRIFT if changes else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
