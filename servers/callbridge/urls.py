from django.urls import path

from . import views

urlpatterns = [
    path('trips/<int:trip_id>/', views.trip_calls),
    path('trips/<int:trip_id>/start/', views.start_call),
    path('sessions/<int:session_id>/end/', views.end_call),
]
