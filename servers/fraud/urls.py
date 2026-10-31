from django.urls import path

from . import views

urlpatterns = [
    path('admin/flags/', views.flag_list, name='fraud_flags'),
]
