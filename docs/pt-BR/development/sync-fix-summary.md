!!! warning "Tradução pendente"
    Esta página ainda não foi traduzida para o português. Conteúdo em inglês abaixo.

# Virtual-Machines Sync Stage HTTP 502 Fix

## Problem

The virtual-machines sync stage was failing after ~2 minutes with:
- **Error**: `RuntimeError("Stage 'virtual-machines' failed (HTTP 502): Response ended prematurely")`
- **Root Cause**: PostgreSQL connection pool exhaustion during backup synchronization

### Evidence

```
[06/Apr/2026 22:37:27-31] Hundreds of concurrent PATCH/POST to /api/plugins/proxbox/backups/
[06/Apr/2026 22:37:30] "Skipping config initialization (database unavailable)"
[06/Apr/2026 22:37:31] HTTP 500 errors for all backup operations
```

NetBox's PostgreSQL connection pool was completely exhausted, preventing any database operations.

## Solution

### Changes Made

#### 1. Environment Variables (proxbox_api/routes/virtualization/virtual_machines/backups_vm.py)

Added configurable backup sync throttling:

```python
_DEFAULT_BACKUP_BATCH_SIZE = max(1, int(os.getenv("PROXBOX_BACKUP_BATCH_SIZE", "5")))
_DEFAULT_BACKUP_BATCH_DELAY_MS = max(0, int(os.getenv("PROXBOX_BACKUP_BATCH_DELAY_MS", "200")))
```

- **PROXBOX_BACKUP_BATCH_SIZE**: Default reduced from 10 → 5
- **PROXBOX_BACKUP_BATCH_DELAY_MS**: Default 200ms inter-batch delay

#### 2. Enhanced process_backups_batch()

```python
async def process_backups_batch(
    backup_tasks: list,
    batch_size: int = 10,
    delay_ms: int = 200,
) -> tuple[list, int]:
```

Improvements:
- ✅ Added configurable `delay_ms` parameter
- ✅ Added `asyncio.sleep(delay_ms / 1000.0)` between batches
- ✅ Progress logging every 10 batches (reduces log spam)
- ✅ Total batch count tracking for progress reporting

#### 3. Caller Updates

Updated `_create_all_virtual_machine_backups()` to use new defaults:

```python
results, failure_count = await process_backups_batch(
    all_backup_tasks,
    batch_size=_DEFAULT_BACKUP_BATCH_SIZE,
    delay_ms=_DEFAULT_BACKUP_BATCH_DELAY_MS,
)
```

#### 4. Documentation (README.md)

Added comprehensive "Backup Sync Throttling" section with:
- Configuration examples
- Tuning guidelines based on NetBox capacity
- Symptom-based troubleshooting guide

## Technical Details

### Why This Fixes the Issue

**Before:**
1. 100+ backups → 10+ batches of 10 concurrent tasks
2. Each batch fires `asyncio.gather(*batch)` immediately
3. All coroutines queue up, even though NetBox semaphore limits concurrency
4. PostgreSQL connection pool (typically 20-40 connections) exhausted
5. NetBox returns HTTP 500 "database unavailable"
6. proxbox-api HTTP stream times out → HTTP 502

**After:**
1. Smaller batches (5 instead of 10) reduce burst pressure
2. 200ms delay between batches allows PostgreSQL connections to release
3. NetBox connection pool stays under capacity
4. Existing retry logic in `netbox_rest.py` handles transient failures
5. Sync completes successfully

### Existing Safety Mechanisms

The fix complements existing protections:

1. **Semaphore in netbox_rest.py** (default concurrency=1):
   ```python
   _netbox_request_semaphore = asyncio.Semaphore(_resolve_netbox_max_concurrent())
   ```

2. **Retry logic with exponential backoff**:
   ```python
   # Up to 30s backoff for "database unavailable" errors
   if _is_netbox_overwhelmed_error(error):
       exponential_delay = max(exponential_delay, 30.0)
   ```

3. **Transient error detection**:
   ```python
   transient_indicators = [
       "database unavailable",
       "too many connections",
       "connection slots are reserved",
       ...
   ]
   ```

## Configuration Guide

### Conservative (Default)
```bash
export PROXBOX_BACKUP_BATCH_SIZE=5
export PROXBOX_BACKUP_BATCH_DELAY_MS=200
```
**Use when**: Standard NetBox deployment, shared database, multiple users

### Aggressive (High-Performance)
```bash
export PROXBOX_BACKUP_BATCH_SIZE=10
export PROXBOX_BACKUP_BATCH_DELAY_MS=50
```
**Use when**: Dedicated NetBox server, large PostgreSQL pool (50+ connections), few concurrent users

### Ultra-Conservative (Troubled Systems)
```bash
export PROXBOX_BACKUP_BATCH_SIZE=3
export PROXBOX_BACKUP_BATCH_DELAY_MS=500
```
**Use when**: Frequent "database unavailable" errors, shared/overloaded database

## Testing Recommendations

1. **Run full sync with 100+ backups**
   ```bash
   # Monitor NetBox logs for "database unavailable"
   tail -f /opt/netbox/netbox.log | grep -i "database\|500\|502"
   ```

2. **Check PostgreSQL connection usage**
   ```sql
   SELECT count(*) FROM pg_stat_activity WHERE datname = 'netbox';
   ```

3. **Verify sync completion**
   - Check NetBox job status shows "Completed" (not "Errored")
   - Confirm backup count matches Proxmox
   - No HTTP 502 errors in logs

## Rollout Impact

- ✅ **Backwards Compatible**: Existing code works unchanged
- ✅ **Conservative Defaults**: Safer out-of-the-box (batch_size 5 vs 10)
- ✅ **No Breaking Changes**: All existing APIs unchanged
- ✅ **Self-Documenting**: Environment variables are clearly named
- ⚠️ **Slight Slowdown**: 200ms/batch adds ~2-4s per 100 backups (acceptable for stability)

## Related Files

- `proxbox_api/routes/virtualization/virtual_machines/backups_vm.py` - Main implementation
- `proxbox_api/netbox_rest.py` - Retry logic and semaphore
- `proxbox_api/utils/retry.py` - Error detection and backoff computation
- `README.md` - User documentation
- `netbox_proxbox/jobs.py` - NetBox job runner (unchanged, uses proxbox-api)

## Future Improvements

1. **Adaptive throttling**: Auto-adjust batch size based on error rate
2. **Metrics collection**: Track batch processing time and failures
3. **Health endpoint**: Expose current batch processing status
4. **Rate limiter**: Global request rate limit across all sync types
