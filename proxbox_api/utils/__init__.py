from .sync_decorator import sync_process

def return_status_html(status: str, use_css: bool) -> str:
    '''
    Return the status of the sync in HTML format.
    
    Args:
        status (str): The status of the sync.
        use_css (bool): Whether to use CSS classes.
        
    Returns:
        str: The status of the sync in HTML format.
    '''
    
    undefined_html_raw = "undefined"
    undefined_html_css = f"<span class='badge text-bg-grey'><strong>{undefined_html_raw}</strong></span>"
    undefined_html = undefined_html_css if use_css else undefined_html_raw
         
    sync_status_html_css = "<span class='text-bg-yellow badge p-1' title='Syncing VM' ><i class='mdi mdi-sync'></i></span>"
    sync_status_html_raw = "syncing"
    sync_status_html = sync_status_html_css if use_css else sync_status_html_raw

    completed_sync_html_css = "<span class='text-bg-green badge p-1' title='Synced VM'><i class='mdi mdi-check'></i></span>"
    completed_sync_html_raw = "completed"
    completed_sync_html = completed_sync_html_css if use_css else completed_sync_html_raw

    if status == "syncing":
        return sync_status_html
    elif status == "completed":
        return completed_sync_html
    return undefined_html