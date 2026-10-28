from typing import Any
from django.contrib.auth.models import UserManager

from servers.admin_audit.access_audit import AccessAuditedQuerySet


class customUserManager(UserManager):
    # The authority flags on a user (`is_staff`, `is_superuser`, `role`,
    # `ops_role`, `is_break_glass`) are access, and every change to them is
    # audited by `servers.admin_audit.access_audit`. Signals cover `save()`;
    # this queryset covers `update()`, `bulk_update()` and `bulk_create()`,
    # which send none. An `update()` that touches no authority field is the
    # plain Django method with no extra query.
    #
    # Only the queryset class changes. The manager keeps its name and
    # module, so `deconstruct()` -- and therefore the migration state, in
    # which `UserManager` subclasses are recorded -- is unchanged.
    _queryset_class = AccessAuditedQuerySet

    def create_user(self, phone_number: str, email: str | None = None, password: str | None = None, **extra_fields: Any) -> Any:
        if not phone_number:
            raise ValueError('Phone number is required')
        
        # Generate username from phone number if not provided
        if 'username' not in extra_fields:
            extra_fields['username'] = phone_number
        
        user = self.model(phone_number=phone_number, email=email, **extra_fields)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user
    def create_superuser(self, phone_number: str, email: str | None = None, password: str | None = None, **extra_fields: Any) -> Any:
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        return self.create_user(phone_number, email, password, **extra_fields)