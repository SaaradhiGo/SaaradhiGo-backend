import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'base.settings')
django.setup()

from django.contrib.auth import get_user_model
from rest_framework_simplejwt.tokens import AccessToken
from servers.driver.models import Driver

User = get_user_model()

print("=" * 60)
print(" SaaradhiGo WebSocket Test Helper")
print("=" * 60)

users = User.objects.all()[:5]
if not users.exists():
    print("No users found in database.")
else:
    print(f"\nFound {User.objects.count()} total users. Showing first few:")
    for u in users:
        token = str(AccessToken.for_user(u))
        is_driver = Driver.objects.filter(user_id=u.id).exists()
        approved = Driver.objects.filter(user_id=u.id, approved=True).exists() if is_driver else False
        role = f"Driver (approved={approved})" if is_driver else "Rider"
        print(f"\n- User ID: {u.id} | Phone: {u.phone_number} | Role: {role}")
        print(f"  Access Token: {token}")
        if is_driver and approved:
            print(f"  Driver Location WS URL:")
            print(f"  ws://localhost:8000/ws/driver/location/?token={token}&lat=17.3850&lng=78.4867")
        else:
            print(f"  Rider Request WS URL:")
            print(f"  ws://localhost:8000/ws/ride/request/?token={token}")

print("=" * 60)
