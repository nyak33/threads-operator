"""Regression tests for Activity document preloader extraction."""

from threads_operator.activity_browser import _extract_preloader_payload


KEY = "BarcelonaActivityFeedStoryListContainerQueryRelayPreloader"


def _block(result_json: str) -> str:
    return f'{{"preloader":"{KEY}","result":{result_json}}}'


def test_extract_preloader_skips_viewer_stub_and_returns_notification_payload():
    document = "\n".join(
        [
            _block('{"data":{"viewer":{"id":"viewer-stub"}}}'),
            _block(
                '{"data":{"notifications":{"edges":['
                '{"node":{"story_type":"follow","args":{"tuuid":"notif-1"}}}'
                ']}}}'
            ),
        ]
    )

    payload = _extract_preloader_payload(document)

    assert payload["data"]["notifications"]["edges"][0]["node"]["args"]["tuuid"] == "notif-1"


def test_extract_preloader_returns_none_when_no_notification_payload_exists():
    document = _block('{"data":{"viewer":{"id":"viewer-stub"}}}')

    assert _extract_preloader_payload(document) is None
