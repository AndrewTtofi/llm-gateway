"""Propose updates to config/pricing.yaml and config/catalog.yaml from public catalogs.

    python -m tools.sync_prices            # show the diff; exit 1 if anything changed
    python -m tools.sync_prices --write    # apply changes both sources agree on

Exit codes: 0 up to date, 1 changes found, 2 the catalogs couldn't be fetched.

No provider publishes prices through an API, so this reads two community catalogs:

- LiteLLM's model list (primary): keyed by the providers' own model IDs, and includes
  context windows and capability flags.
- OpenRouter's /models API (cross-check): its own IDs, prices per token.

Prices drive budgets and spend reports, so nothing changes silently. The diff is printed
for review, and a price the two sources disagree on is flagged and not written unless
`--force` is given. Quality scores in catalog.yaml are the operator's and are never changed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

LITELLM_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)
OPENROUTER_URL = "https://openrouter.ai/api/v1/models"
PRICE_FIELDS = ("input", "output", "cached_input")
CAPABILITIES = {  # LiteLLM flag → catalog capability
    "supports_function_calling": "tools",
    "supports_vision": "vision",
    "supports_reasoning": "reasoning",
    "supports_response_schema": "json_schema",
}
SKIP_TYPES = {"fake"}  # never priced from public sources
TOLERANCE = 0.005  # relative difference treated as equal (rounding in the sources)


@dataclass
class Change:
    target: str
    field: str
    current: Any
    proposed: Any
    check: Any = None  # the cross-check source's value, if it has one
    agreed: bool = True  # False: the sources disagree, so it needs a human decision

    def row(self) -> str:
        flag = "" if self.agreed else "  ⚠ sources disagree"
        check = f"  (openrouter: {self.check})" if self.check is not None else ""
        change = f"{self.current!s:>12} → {self.proposed!s:<12}"
        return f"{self.target:42} {self.field:18} {change}{check}{flag}"


def per_million(per_token: Any) -> float | None:
    if per_token in (None, ""):
        return None
    return round(float(per_token) * 1_000_000, 6)


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


def propose(
    target: str,
    litellm: dict[str, Any],
    openrouter: dict[str, Any],
    price: dict[str, Any],
    entry: dict[str, Any],
) -> list[Change]:
    model = target.partition("/")[2]
    src = litellm.get(entry.get("litellm_id") or model) or litellm.get(target)
    if not src:
        return []
    check = openrouter.get(entry.get("openrouter_id") or openrouter_id(target))
    check_prices = (check or {}).get("pricing") or {}
    proposed = {
        "input": per_million(src.get("input_cost_per_token")),
        "output": per_million(src.get("output_cost_per_token")),
        "cached_input": per_million(src.get("cache_read_input_token_cost")),
    }
    checked = {
        "input": per_million(check_prices.get("prompt")),
        "output": per_million(check_prices.get("completion")),
        "cached_input": per_million(check_prices.get("input_cache_read")),
    }
    out: list[Change] = []
    for f in PRICE_FIELDS:
        new, cur, other = proposed[f], price.get(f), checked[f]
        if new is None or same(new, cur):
            continue
        out.append(Change(target, f, cur, new, other, other is None or same(new, other)))

    facts = {
        "context_window": src.get("max_input_tokens"),
        "max_output_tokens": src.get("max_output_tokens"),
        "capabilities": sorted(c for flag, c in CAPABILITIES.items() if src.get(flag)),
    }
    for f, new in facts.items():
        if new in (None, []) or new == entry.get(f):
            continue
        out.append(Change(target, f, entry.get(f), new))
    return out


# --- writing -----------------------------------------------------------------


def _price(v: float) -> str:
    """At least two decimals, like the hand-written file: 10.00, 0.10, 0.125."""
    whole, _, frac = f"{v:.6f}".rstrip("0").partition(".")
    return f"{whole}.{frac.ljust(2, '0')}"


def _flow(values: dict[str, Any], prices: bool = False) -> str:
    def fmt(v: Any) -> str:
        if prices and isinstance(v, int | float) and not isinstance(v, bool):
            return _price(float(v))
        if isinstance(v, list):
            return "[" + ", ".join(map(str, v)) + "]"
        return str(v)

    return "{ " + ", ".join(f"{k}: {fmt(v)}" for k, v in values.items()) + " }"


def write_pricing(path: Path, changes: list[Change], today: str) -> None:
    """Rewrite only the changed lines (one model per line), keeping comments."""
    text = path.read_text()
    current = yaml.safe_load(text).get("models", {})
    by_target: dict[str, dict[str, Any]] = {}
    for c in changes:
        if c.field in PRICE_FIELDS:
            by_target.setdefault(c.target, dict(current.get(c.target) or {}))[c.field] = c.proposed
    lines = text.splitlines()
    for target, values in by_target.items():
        ordered = {f: values[f] for f in PRICE_FIELDS if values.get(f) is not None}
        # Keep the indent, the column alignment after the colon, and any trailing comment.
        pattern = re.compile(rf"^(\s*){re.escape(target)}:(\s*)\{{[^}}]*\}}(.*)$")
        for i, line in enumerate(lines):
            if m := pattern.match(line):
                flow = _flow(ordered, prices=True)
                lines[i] = f"{m.group(1)}{target}:{m.group(2)}{flow}{m.group(3)}"
                break
        else:  # a target that had no price yet
            lines.append(
                f"  {target}: {_flow(ordered, prices=True)}  # added by sync_prices {today}"
            )
    out = "\n".join(lines) + "\n"
    out = re.sub(r"# Last checked: [0-9-]+", f"# Last checked: {today}", out, count=1)
    path.write_text(out)


CATALOG_HEADER = """\
# Model facts for GET /v1/catalog (and, later, policy routing). ADR 0011.
# context_window / max_output_tokens / capabilities: synced by `make prices` from public
# catalogs — review the diff before committing. quality: YOUR score, 1 (basic) to 5
# (frontier), for your own use cases; the sync never changes it.
# Optional per model: litellm_id / openrouter_id when the source uses a different ID.
"""


def write_catalog(path: Path, catalog: dict[str, Any], changes: list[Change], today: str) -> None:
    models: dict[str, dict[str, Any]] = {
        t: dict(v or {}) for t, v in (catalog.get("models") or {}).items()
    }
    for c in changes:
        if c.field not in PRICE_FIELDS:
            models.setdefault(c.target, {})[c.field] = c.proposed
    order = ("context_window", "max_output_tokens", "capabilities", "quality")
    lines = [CATALOG_HEADER, f"checked: {today}", "models:"]
    for t in sorted(models):
        v = models[t]
        ordered = {k: v[k] for k in order if k in v} | {k: v[k] for k in v if k not in order}
        lines.append(f"  {t}: {_flow(ordered)}")
    path.write_text("\n".join(lines) + "\n")


# --- CLI ---------------------------------------------------------------------


def fetch() -> tuple[dict[str, Any], dict[str, Any]]:
    with httpx.Client(timeout=60, follow_redirects=True) as http:
        litellm = http.get(LITELLM_URL).raise_for_status().json()
        data = http.get(OPENROUTER_URL).raise_for_status().json()["data"]
    return litellm, {m["id"]: m for m in data}


def diff(
    config_dir: Path, litellm: dict[str, Any], openrouter: dict[str, Any]
) -> tuple[list[Change], list[str]]:
    models_yaml = yaml.safe_load((config_dir / "models.yaml").read_text())
    pricing = yaml.safe_load((config_dir / "pricing.yaml").read_text())
    catalog_path = config_dir / "catalog.yaml"
    catalog = yaml.safe_load(catalog_path.read_text()) if catalog_path.exists() else {}
    changes: list[Change] = []
    missing: list[str] = []
    for t in targets(models_yaml, pricing):
        price = (pricing.get("models") or {}).get(t) or {}
        entry = ((catalog or {}).get("models") or {}).get(t) or {}
        found = propose(t, litellm, openrouter, price, entry)
        if not found and not (litellm.get(entry.get("litellm_id") or t.partition("/")[2])):
            missing.append(t)
        changes += found
    return changes, missing


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--config-dir", type=Path, default=Path("config"))
    p.add_argument("--write", action="store_true", help="apply changes the sources agree on")
    p.add_argument("--force", action="store_true", help="with --write: also apply disputed ones")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    args = p.parse_args(argv)

    try:
        litellm, openrouter = fetch()
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        print(f"could not fetch the price catalogs: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2  # not drift: nothing was compared
    changes, missing = diff(args.config_dir, litellm, openrouter)
    if args.json:
        print(
            json.dumps({"changes": [c.__dict__ for c in changes], "no_source": missing}, indent=2)
        )
    else:
        for c in changes:
            print(c.row())
        if missing:
            print(f"\nno public source (kept as is): {', '.join(missing)}")
        if not changes:
            print("pricing and catalog are up to date")
    if args.write and changes:
        apply = [c for c in changes if c.agreed or args.force]
        today = dt.date.today().isoformat()
        write_pricing(args.config_dir / "pricing.yaml", apply, today)
        catalog_path = args.config_dir / "catalog.yaml"
        catalog = yaml.safe_load(catalog_path.read_text()) if catalog_path.exists() else {}
        write_catalog(catalog_path, catalog or {}, apply, today)
        skipped = len(changes) - len(apply)
        print(
            f"\nwrote {len(apply)} change(s)"
            + (f"; {skipped} disputed, skipped" if skipped else "")
        )
        print("review with `git diff config/`, then `make reload`")
    return 1 if changes else 0


if __name__ == "__main__":
    sys.exit(main())
