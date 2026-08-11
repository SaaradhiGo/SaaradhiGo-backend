from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from .models import Driver

def list_drivers_admin(page_number=1, approved=None, driver_status=None):
    """
    List all drivers. Can be filtered by 'approved' and 'status'.
    Returns a Django Page object for easy template rendering.
    """
    drivers = Driver.objects.all().select_related('user_id').order_by('-id')
    
    if approved is not None:
        approved_bool = str(approved).lower() in ['true', '1', 't', 'y', 'yes']
        drivers = drivers.filter(approved=approved_bool)
        
    if driver_status and driver_status != 'ALL':
        drivers = drivers.filter(status=driver_status)

    paginator = Paginator(drivers, 10) # 10 items per page
    try:
        page = paginator.page(page_number)
    except PageNotAnInteger:
        page = paginator.page(1)
    except EmptyPage:
        page = paginator.page(paginator.num_pages)
        
    return page
