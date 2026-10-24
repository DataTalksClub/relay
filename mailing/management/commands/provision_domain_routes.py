"""Idempotent, audited route provisioning from a secret-free JSON manifest."""
import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from mailing.models import Client, InboundDomain, InboundMailbox
from mailing.services.operator_management import audit


class Command(BaseCommand):
    help = 'Validate a route manifest; write only with --apply. Secret values are never accepted.'

    def add_arguments(self, parser):
        parser.add_argument('manifest')
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **options):
        try:
            with open(options['manifest']) as stream:
                manifest = json.load(stream)
            with transaction.atomic():
                count = 0
                for spec in manifest['domains']:
                    client = Client.objects.get(slug=spec['client'])
                    domain = InboundDomain.objects.filter(name=spec['name']).first() or InboundDomain(name=spec['name'])
                    domain.client = client
                    domain.from_email = spec['from_email']
                    domain.full_clean()
                    domain.save()
                    audit(None, 'inbound_routing.provision', domain, {'source': 'provision_domain_routes'})
                    for fields in spec['mailboxes']:
                        allowed = {'local_part', 'kind', 'destinations', 'sender_allowlist', 'webhook_secret_id', 'enabled', 'rate_per_minute'}
                        if set(fields) - allowed:
                            raise CommandError('Unknown mailbox fields; manifest may contain secret values.')
                        row = InboundMailbox.objects.filter(domain=domain, local_part=fields['local_part']).first() or InboundMailbox(domain=domain)
                        for name, value in fields.items():
                            setattr(row, name, value)
                        row.full_clean()
                        row.save()
                        audit(None, 'inbound_routing.provision', row, {'source': 'provision_domain_routes', 'fields': sorted(fields)})
                        count += 1
                if not options['apply']:
                    transaction.set_rollback(True)
                self.stdout.write(f'{count} mailbox rules validated; applied={options["apply"]}')
        except CommandError:
            raise
        except Exception as exc:
            # Validation errors must not echo supplied fields or credentials.
            raise CommandError(f'Route manifest rejected ({type(exc).__name__}); no changes committed.') from None
