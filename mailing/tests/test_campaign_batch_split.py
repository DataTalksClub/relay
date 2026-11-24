import pytest

from mailing.services.campaigns import campaign_batch_size


@pytest.fixture
def split_settings(settings):
    settings.CAMPAIGN_EMAIL_BATCH_SIZE = 500
    settings.CAMPAIGN_MIN_BATCH_TASKS = 12
    settings.CAMPAIGN_MIN_BATCH_SIZE = 50
    return settings


def _tasks(count, size):
    return -(-count // size)


def test_small_campaign_spreads_over_all_workers(split_settings):
    # Campaign 321: 1,416 recipients went out as 3 tasks of 500 on 3 of 6 workers.
    size = campaign_batch_size(1416)
    assert size == 118
    assert _tasks(1416, size) == 12


def test_large_campaign_keeps_configured_batch_size(split_settings):
    assert campaign_batch_size(131_415) == 500
    assert campaign_batch_size(6000) == 500


def test_tiny_campaign_respects_minimum_batch_size(split_settings):
    assert campaign_batch_size(120) == 50
    assert campaign_batch_size(10) == 50
    assert _tasks(10, campaign_batch_size(10)) == 1


def test_explicit_batch_size_wins(split_settings):
    assert campaign_batch_size(1416, 500) == 500
    assert campaign_batch_size(1416, 7) == 7


def test_split_disabled_with_one_task(split_settings):
    split_settings.CAMPAIGN_MIN_BATCH_TASKS = 1
    assert campaign_batch_size(1416) == 500


def test_minimum_never_exceeds_configured_size(split_settings):
    split_settings.CAMPAIGN_EMAIL_BATCH_SIZE = 40
    assert campaign_batch_size(1416) == 40
    assert campaign_batch_size(0) == 40
