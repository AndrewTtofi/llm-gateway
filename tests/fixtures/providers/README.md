# Provider fixtures

Complete provider responses, replayed through the adapters by `tests/test_golden.py`
(ADR 0026). They carry every field the providers document, not just the ones the
gateway reads, so a translator that trips over a real-world field shows up here.

They're written from each provider's published API reference (formats as of October
2026), not recorded from live calls: recording costs money, and none was spent yet.
To replace them with real recordings once keys can be used, run:

```bash
python tools/record_fixtures.py      # needs the provider API keys; a few cents
```

which overwrites these files with real responses, with identifiers redacted.
