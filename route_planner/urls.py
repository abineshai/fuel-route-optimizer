from django.urls import path
from .views import FuelRouteView

urlpatterns = [
    path("route/", FuelRouteView.as_view(), name="fuel-route"),
]
