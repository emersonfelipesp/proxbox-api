# proxbox-reconcile-rs

Optional native reconciliation engine for `proxbox-api`.

This package is intentionally independent from the Python API package. The
Python backend can use it through `PROXBOX_RECONCILIATION_ENGINE=compare` or
`PROXBOX_RECONCILIATION_ENGINE=rust`, but Python remains the default engine.

## Local Development

```bash
cargo test --no-default-features
maturin develop --release
python -c "import proxbox_reconcile_rs; print(proxbox_reconcile_rs.engine_version())"
```

After wheels are published, `proxbox-api` can add a normal optional dependency
extra. Until then, install this package locally from `proxbox-reconcile-rs/`.
