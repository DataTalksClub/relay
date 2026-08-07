from django.contrib import admin
from django.urls import include, path

from relay import oidc

urlpatterns = [
    path("auth/login", oidc.begin, name="oidc-login"),
    path("auth/callback", oidc.callback, name="oidc-callback"),
    path("auth/error", oidc.auth_error, name="oidc-error"),
    path("auth/logout", oidc.end, name="oidc-logout"),
    path("admin/login/", oidc.admin_login, name="shared-admin-login"),
    path("admin/", admin.site.urls),
    path("", include("jobs.urls")),
    path("", include("mailing.urls")),
]
