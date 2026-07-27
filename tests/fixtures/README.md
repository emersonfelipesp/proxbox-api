# Cloud Image Contract Fixtures

These JSON files are producer-side regression evidence for the versioned Cloud
Image preflight contract:

- `packer_preflight_v1.json` is parsed by proxbox-api's own v1 response model.
- `netbox_packer_preflight_v1.json` is a **producer-owned**, consumer-shaped
  example derived from the pending netbox-packer integration requirements. An
  independent test-only Pydantic model parses it without importing the producer
  response types.

The second fixture is not downstream conformance evidence and must not be
described as validation by netbox-packer. Cloud Image remote execution remains
disabled by default, and staging/production operators must keep
`PROXBOX_ENABLE_CLOUD_IMAGE_EXECUTION` unset or false until netbox-packer lands
and validates its own consumer contract against the released producer API.
