from celery import shared_task


@shared_task(name='platform_ops.integrity_scan', max_retries=0)
def integrity_scan():
    """Daily data integrity check (10 Nov); raises a console alert on findings, fixes nothing."""
    from .integrity import scan

    result = scan(fix=False)
    return {'found': result['total_found']}
