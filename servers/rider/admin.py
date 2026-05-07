from django.contrib import admin
from .models import Rider,FavoritePlace,Wallet,Notification,WalletTransaction
# Register your models here.
admin.site.register(Rider)
admin.site.register(FavoritePlace)
admin.site.register(Wallet)
admin.site.register(Notification)
admin.site.register(WalletTransaction)