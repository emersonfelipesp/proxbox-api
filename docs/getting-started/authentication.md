# Authentication

`proxbox-api` uses database-backed API key authentication. All API keys are stored in the SQLite database with bcrypt hashing. There is no environment variable authentication — all key management happens through the API endpoints.

## Bootstrap Flow

When the backend starts with an empty database, it returns `needs_bootstrap: true` from the status endpoint:

```bash
curl http://localhost:8800/auth/bootstrap-status
# {"needs_bootstrap": true, "has_db_keys": false}
```

### First Key Registration

The first API key can be registered without authentication (bootstrap mode):

```bash
curl -X POST http://localhost:8800/auth/register-key \
  -H "Content-Type: application/json" \
  -d '{"api_key": "your-secure-api-key-at-least-32-chars", "label": "bootstrap-key"}'
# {"detail": "API key registered."}
```

Once a key exists, subsequent calls to `/auth/register-key` return `409 Conflict`.

### NetBox Plugin Integration

When you save a `FastAPIEndpoint` in NetBox, the plugin automatically:

1. Generates a 64-character secure token
2. Calls `/auth/bootstrap-status` to check if registration is needed
3. Calls `/auth/register-key` to register the token with the backend
4. Stores the token in NetBox for future authenticated requests

## Using API Keys

All requests (except bootstrap endpoints) require the `X-Proxbox-API-Key` header:

```bash
curl http://localhost:8800/proxmox/endpoints \
  -H "X-Proxbox-API-Key: your-secure-api-key-at-least-32-chars"
```

## Auth-Exempt Endpoints

These endpoints do not require authentication:

| Endpoint | Purpose |
|----------|---------|
| `GET /` | Root metadata |
| `GET /docs` | OpenAPI documentation |
| `GET /redoc` | ReDoc documentation |
| `GET /openapi.json` | OpenAPI schema |
| `GET /health` | Health check |
| `GET /meta` | Service metadata |
| `GET /auth/bootstrap-status` | Check if bootstrap is needed |
| `POST /auth/register-key` | Register first key (only when no keys exist) |

## Key Management Endpoints

All key management endpoints require authentication:

### List API Keys

```bash
curl http://localhost:8800/auth/keys \
  -H "X-Proxbox-API-Key: your-key"
# {"keys": [{"id": 1, "label": "bootstrap-key", "is_active": true, "created_at": 1712345678.123}]}
```

### Create a New Key

```bash
curl -X POST http://localhost:8800/auth/keys \
  -H "X-Proxbox-API-Key: your-key"
# {"id": 2, "label": "", "is_active": true, "created_at": 1712345678.456, "raw_key": "the-newly-generated-key"}
```

The `raw_key` is only returned once — store it securely.

### Deactivate a Key

```bash
curl -X POST http://localhost:8800/auth/keys/1/deactivate \
  -H "X-Proxbox-API-Key: your-key"
# {"id": 1, "label": "bootstrap-key", "is_active": false, "created_at": 1712345678.123}
```

### Reactivate a Key

```bash
curl -X POST http://localhost:8800/auth/keys/1/activate \
  -H "X-Proxbox-API-Key: your-key"
# {"id": 1, "label": "bootstrap-key", "is_active": true, "created_at": 1712345678.123}
```

### Delete a Key

```bash
curl -X DELETE http://localhost:8800/auth/keys/1 \
  -H "X-Proxbox-API-Key: your-key"
# (204 No Content)
```

## Brute-Force Protection

The backend implements IP-based lockout:

- Maximum 5 failed attempts
- 5-minute lockout duration
- Lockout is cleared on successful authentication

## Security Best Practices

1. **Use strong keys**: At least 32 characters, preferably 64 characters
2. **Store keys securely**: Treat the `raw_key` from `/auth/keys` as a password — store it once
3. **Rotate keys regularly**: Create a new key, update your applications, delete the old one
4. **Use HTTPS in production**: Keys are sent in headers — protect them in transit
5. **Limit key scope**: Create separate keys for different purposes (monitoring, sync, admin)

## Troubleshooting

### "No API key configured"

```
{"detail": "No API key configured. Register a key via POST /auth/register-key or use an existing key."}
```

The database has no API keys. Call `/auth/register-key` with a new key to bootstrap.

### "Invalid API key"

Check that:

1. You're sending the `X-Proxbox-API-Key` header
2. The key value matches exactly (no extra spaces or newlines)
3. The key hasn't been deactivated or deleted

### "Too many failed authentication attempts"

Wait 5 minutes for the lockout to expire, or restart the backend to reset the in-memory lockout state.