"""Inspect durable DMARC evidence without depending on ingress S3 retention."""
import json

from django.core.management.base import BaseCommand, CommandError

from mailing.dmarc_api import serialize_archive
from mailing.models import DmarcArchive


class Command(BaseCommand):
    help = 'List DMARC archives or show parsed aggregate records (--id). Read-only.'

    def add_arguments(self, parser):
        parser.add_argument('--id', type=int)
        parser.add_argument('--domain')
        parser.add_argument('--limit', type=int, default=100)

    def handle(self, *args, **options):
        rows = DmarcArchive.objects.select_related('domain').defer('raw_eml').prefetch_related('attachments__reports__records')
        if options['domain']:
            rows = rows.filter(domain__name=options['domain'].lower())
        if options['id'] is not None:
            row = rows.filter(pk=options['id']).first()
            if row is None:
                raise CommandError('DMARC archive not found')
            result = serialize_archive(row, detail=True)
        else:
            result = {'results': [serialize_archive(row) for row in rows.order_by('-pk')[:max(1, min(options['limit'], 1000))]]}
        self.stdout.write(json.dumps(result))
