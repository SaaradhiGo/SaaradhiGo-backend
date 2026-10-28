from typing import Any
from django.contrib.auth.models import UserManager

from servers.admin_audit.access_audit import AccessAuditedQuerySet


class customUserQuerySet(AccessAuditedQuerySet):
    """The user queryset: audited bulk writes, and no deleting of staff or
    of anyone who appears as an audit actor.

    `delete()` refuses BEFORE the collector starts, so a bulk delete that
    would have removed a thousand riders and one operator removes nothing
    and names the one. See `servers.auth_user.erasure`.
    """

    def delete(self):
        from servers.auth_user import erasure

        erasure.refuse_if_any_undeletable(self)
        return super().delete()

    delete.alters_data = True
    delete.queryset_only = True


class customUserManager(UserManager):
    # The authority flags on a user (`is_staff`, `is_superuser`, `role`,
    # `ops_role`, `is_break_glass`) are access, and every change to them is
    # audited by `servers.admin_audit.access_audit`. Signals cover `save()`;
    # this queryset covers `update()`, `bulk_update()` and `bulk_create()`,
    # which send none. An `update()` that touches no authority field is the
    # plain Django method with no extra query. Its `delete()` refuses staff
    # and audit actors (owner ruling, 2026-10-27).
    #
    # Only the queryset class changes. The manager keeps its name and
    # module, so `deconstruct()` -- and therefore the migration state, in
    # which `UserManager` subclasses are recorded -- is unchanged.
    _queryset_class = customUserQuerySet

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