from functools import wraps
from datetime import datetime
import traceback
from proxbox_api.exception import ProxboxException

def sync_process(sync_type: str):
    '''
    Decorator to create a sync process and track the status of the sync.
    
    Args:
        sync_type (str): The type of sync to track.
        
    Returns:
        Decorator function that wraps the original function.
    '''
    
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Extract expected arguments from args/kwargs
            netbox_session = kwargs.get('netbox_session')
            tag = kwargs.get('tag')
            websocket = kwargs.get('websocket')
            
            # Get the current time in the format YYYY-MM-DD HH:MM:SS
            start_time = datetime.now()
            start_time_str: str = start_time.strftime('%Y-%m-%d %H:%M:%S')
            
            sync_process = None
            
            if any(arg is None for arg in [netbox_session, tag]):
                raise ProxboxException(
                    message=f"Missing required arguments for {sync_type} sync",
                    detail="netbox_session and tag are required"
                )
            
            try:
                tag_id = getattr(tag, 'id', 0)
                tags = [tag_id] if tag_id > 0 else []

                sync_process = netbox_session.plugins.proxbox.__getattr__('sync-processes').create({
                    'name': f"sync-{sync_type}-{start_time_str}",
                    'sync_type': sync_type,
                    'status': "not-started",
                    'started_at': start_time_str,
                    'completed_at': None,
                    'runtime': None,
                    'tags': tags,
                })

                # Add to kwargs to make it accessible in the function
                kwargs['sync_process'] = sync_process

                result = await func(*args, **kwargs)

                # Finish the sync
                end_time = datetime.now()
                end_time_str: str = end_time.strftime('%Y-%m-%d %H:%M:%S')
                sync_process.status = "completed"
                sync_process.completed_at = end_time_str
                sync_process.runtime = float((end_time - start_time).total_seconds())
                sync_process.save()

                if websocket:
                    await websocket.send_json({'object': sync_type, 'end': True})

                return result
            
            except Exception as error:
                traceback.print_exc()
                if sync_process:
                    sync_process.status = "failed"
                    sync_process.save()
                raise ProxboxException(
                    message=f"Error during {sync_type} sync",
                    detail=str(error)
                )

        return wrapper
    return decorator