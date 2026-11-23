"""Test trips and test accounts never count in a figure (owner, 22 Nov).

"Exclude test trips and test accounts from every figure (flag them in the data and filter
centrally)." A test trip is a seed trip (`Trip.is_seed`) or one whose rider or driver account is
listed in `TestAccount` (`manage.py mark_test_accounts`). Every overview and dashboard number
reads trips through `real_trips`.
"""


def real_trips(queryset):
    """`queryset` (of Trip) without seed trips or trips by test accounts."""
    return (queryset.exclude(is_seed=True)
            .exclude(user_id__test_account__isnull=False)
            .exclude(driver_id__user_id__test_account__isnull=False))
