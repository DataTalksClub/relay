"""Pick the SES account, region, and configuration set from the From domain.

The production host sends datatalks.club and aishippinglabs.com with its own
task role. Sandbox domains stay verified in the sandbox account, so those
addresses assume that account's send role and call SES in us-east-1.
"""

from dataclasses import dataclass
from email.utils import parseaddr

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


@dataclass(frozen=True)
class SesRoute:
    role_arn: str
    region: str
    configuration_set: str


def parse_ses_domain_routes(raw):
    routes = {}
    for entry in (raw or "").replace("\n", ";").split(";"):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split()
        domain = parts[0].lower()
        fields = {}
        for part in parts[1:]:
            key, separator, value = part.partition("=")
            if not separator or not key or not value:
                raise ImproperlyConfigured(f"SES domain route {entry!r} has an invalid field.")
            fields[key] = value
        missing = [key for key in ("role", "region", "configuration_set") if key not in fields]
        if missing or "@" in domain or not domain:
            raise ImproperlyConfigured(f"SES domain route {entry!r} is incomplete.")
        routes[domain] = SesRoute(fields["role"], fields["region"], fields["configuration_set"])
    return routes


def domain_of(source):
    _name, address = parseaddr(source or "")
    if "@" not in address:
        return ""
    return address.rsplit("@", 1)[1].strip().lower()


def route_for_source(source):
    return parse_ses_domain_routes(settings.RELAY_SES_DOMAIN_ROUTES).get(domain_of(source))


def configuration_set_for_source(source):
    route = route_for_source(source)
    if route is None:
        return settings.AWS_SES_CONFIGURATION_SET
    return route.configuration_set
