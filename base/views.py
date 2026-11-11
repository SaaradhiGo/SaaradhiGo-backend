from django.shortcuts import render


def bad_request_view(request, exception=None):
    return render(request, 'errors/400.html', status=400)


def permission_denied_view(request, exception=None):
    return render(request, 'errors/403.html', status=403)


def page_not_found_view(request, exception=None):
    return render(request, 'errors/404.html', status=404)
