# Custom Field Retirement

Proxbox reflection state is stored in the typed `Proxbox*SyncState` models
provided by netbox-proxbox. `proxbox-api` no longer creates, reconciles, reads,
or writes NetBox custom fields.

This is a breaking API change. The former
`POST /extras/custom-fields/reconcile` and
`GET /extras/extras/custom-fields/create` routes have been removed, together
with the `custom_fields_enabled` and `custom_fields_request_delay` settings.
Callers must use the normal synchronization routes and the typed sidecar APIs.

The backend does not delete existing custom-field definitions or stored values.
Operators who still have historical fields can remove them through their normal
NetBox change-management process after confirming that every deployed Proxbox
component uses typed sidecars.

`GET /extras/bootstrap-status` remains available. It reports bootstrap results
for the native NetBox support objects that synchronization still owns.
