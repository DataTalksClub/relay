from django.conf import settings
from django.contrib.auth import get_user_model, login


class DevAutoLoginMiddleware:
    """Pre-authenticate requests as the seeded superuser while DEBUG is on.

    Deployed environments go through the OIDC provider (relay.oidc); this
    exists only so a local runserver does not put the Django admin login
    between the developer and the operator UI. DEBUG is False for the whole
    test suite, so the middleware is dormant there.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if settings.DEBUG and not request.user.is_authenticated:
            user = (
                get_user_model()
                .objects.filter(is_active=True, is_superuser=True)
                .order_by("pk")
                .first()
            )
            if user is not None:
                login(request, user)
        return self.get_response(request)
