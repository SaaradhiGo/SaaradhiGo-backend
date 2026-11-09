from django.urls import path

from . import views

urlpatterns = [
    path('contacts/', views.contacts),
    path('contacts/<int:contact_id>/', views.contact_detail),
    path('settings/', views.sharing_settings),
    path('trips/<int:trip_id>/share/', views.trip_share),
    path('public/<str:token>/', views.public_trip),
    path('sms/inbound/', views.sms_inbound),
]
