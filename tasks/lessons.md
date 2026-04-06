# Lessons Learned

## 2026-04-06

- When CI protocol requirements mention both directions (A over HTTPS vs B over HTTP and the inverse), design the matrix as transport pairs, not single-service permutations.
- Treat TLS verification requirements as explicit acceptance criteria: include CA trust bootstrap in CI and avoid any `verify_ssl=False` shortcuts in validation paths.
