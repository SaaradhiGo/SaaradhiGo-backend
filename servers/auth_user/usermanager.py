from typing import Any
from django.contrib.auth.models import UserManager
class customUserManager(UserManager):
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