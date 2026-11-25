from celery import shared_task


@shared_task(name='approvals.escalation_sweep')
def escalation_sweep():
    """25 Nov: escalate requests waiting over 3 days to Super Admin; lapse the overdue ones."""
    from . import escalation, service

    expired = service.expire_stale()
    raised, cleared = escalation.sweep()
    return {'expired': expired, 'escalated': raised, 'cleared': cleared}
