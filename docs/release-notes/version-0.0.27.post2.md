# proxbox-api 0.0.27.post2

## Summary

This post-release fixes a production outage in which every Proxbox
synchronization after the first one failed with HTTP 503 "Plugin
encryption-key authorization is unavailable" until the backend was restarted.
It adds no features and no database migrations.

## Fix

- **Unchanged NetBox endpoint updates are a no-op.** netbox-proxbox pushes its
  NetBox endpoint to the backend before every synchronization. Each
  `PUT /netbox/endpoint/{id}` used to re-encrypt the stored token, commit, and
  invalidate the cached NetBox clients, even when nothing had changed. That
  invalidation retires the selected plugin encryption-key source, which then
  stays blocked by design until an explicit reselection. As a result, once the
  plugin key had been selected, the next push made every key request fail until
  the process restarted.

  An update now compares the connection and credential fields before and after
  the request. Stored secrets are decrypted for that comparison because
  encryption is not deterministic, and the decryption runs in a worker thread so
  that it cannot block the event loop. When nothing changed, the backend
  discards the in-memory changes and returns the endpoint without
  re-encrypting, committing, or invalidating anything.

## Unchanged behavior and known limitation

- Creating, deleting, or really changing the NetBox endpoint still invalidates
  the clients and keeps the selected plugin-key source blocked until restart.
  This protects against a credential swap.
- When the plugin key is the selected source, a changed endpoint credential is
  re-encrypted under that plugin key. The backend can then fail to start,
  because it needs that same credential to fetch the key. Give the backend its
  own encryption key (environment variable or local key file) before you change
  the NetBox endpoint credentials. A later release will address this.

## Upgrade

Deploy the exact `proxbox-api 0.0.27.post2` package or container through the
approved release workflow and restart every backend worker. Then run two
consecutive synchronizations from netbox-proxbox and confirm that both pass the
backend endpoint push and the plugin-key read.
