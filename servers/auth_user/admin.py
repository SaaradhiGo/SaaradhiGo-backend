from django.contrib import admin
from django.contrib.auth import get_user_model
# Register your models here.
user_model=get_user_model()
admin.site.register(user_model)