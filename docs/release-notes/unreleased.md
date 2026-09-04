# Unreleased

## Breaking changes

- Removes Proxbox's legacy NetBox custom-field inventory, reconciliation
  service, bootstrap integration, payload writes, response fallbacks, and local
  cache handling. Reflection state now uses only the typed
  `Proxbox*SyncState` sidecars supplied by netbox-proxbox.
- Removes `POST /extras/custom-fields/reconcile` and the legacy
  `GET /extras/extras/custom-fields/create` route. Clients that call either
  endpoint must stop doing so before upgrading.
- Removes the `custom_fields_enabled` and `custom_fields_request_delay`
  settings from the backend settings contract.

The upgrade does not delete existing NetBox custom-field definitions or stored
values. Operators can remove historical fields separately after verifying that
all deployed integrations use typed sidecars.
