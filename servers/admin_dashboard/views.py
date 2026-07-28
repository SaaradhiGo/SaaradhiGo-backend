from django.shortcuts import render,redirect

# Create your views here.
def login(request):
    return render(request, 'admin_pages/login.html')
def dashboard(request):
    return render(request, 'admin_pages/fleet_monitor.html')
def driver_onboarding(request):
    return render(request, 'admin_pages/driver_onboarding.html')
def dispute_support(request):
    return render(request, 'admin_pages/dispute_support.html')
def payment_dashboard(request):
    return render(request, 'admin_pages/payment_dashboard.html')
def executive_revenue(request):
    return render(request, 'admin_pages/executive_revenue.html')
def driver_loyalty(request):
    return render(request, 'admin_pages/driver_loyalty.html')
def fare_surge(request):
    return render(request,"admin_pages/fare_surge.html")
def predictive_heatmaps(request):
    return render(request,'admin_pages/predictive_heatmaps.html')
def logout(request):
    return redirect("login")