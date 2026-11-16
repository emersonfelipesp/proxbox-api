# Unreleased

## Fixed

- **NetBox OpenAPI schema is downloaded one time, not one time for each virtual
  machine.** During a virtual machine sync, payload validation resolved the
  NetBox schema contract for every guest, and each resolution downloaded and
  parsed NetBox's complete OpenAPI document (about 16 MB on a production
  instance). A sync of 46 guests made 46 requests to `/api/schema/`. The
  proxbox-api process kept hundreds of megabytes of resident memory after each
  sync, and NetBox had to generate its schema again for each request. The
  resolved contract is now reused inside the process and concurrent workers
  share one download. `PROXBOX_NETBOX_SCHEMA_CONTRACT_TTL` (default `3600`
  seconds, `0` disables reuse) sets how long the contract is reused.
