from threads_operator import operator_cli


class FakeConfig:
    name = "syaqir"

    def get_bool(self, key):
        if key == "THREADS_POSTING_ENABLED":
            return True
        return False

    def get(self, key, default=None):
        values = {
            "THREADS_EXECUTION_MODE": "auto_post",
            "THREADS_QUEUE_TABLE": "threads_publish_queue",
            "THREADS_QUEUE_CAMPAIGN_CODE": "",
        }
        return values.get(key, default)


class FakeAPI:
    def __init__(self):
        self.enabled = False

    def enable_publishing(self):
        self.enabled = True


def test_live_cli_grants_publish_capability_only_after_live_gates(monkeypatch):
    api = FakeAPI()
    monkeypatch.setattr(operator_cli, "_api", lambda config: api)
    monkeypatch.setattr(operator_cli, "_store", lambda config: object())

    def fake_publish(received_api, store, table, campaign_code=None, dry_run=False):
        assert received_api is api
        assert api.enabled is True
        assert dry_run is False
        return {"status": "posted", "queue_id": 7}

    monkeypatch.setattr(operator_cli, "publish_next", fake_publish)

    code, result = operator_cli._run_publish(FakeConfig(), dry_run=False)

    assert code == 0
    assert result["status"] == "posted"


def test_dry_run_never_grants_public_publish_capability(monkeypatch):
    api = FakeAPI()
    monkeypatch.setattr(operator_cli, "_api", lambda config: api)
    monkeypatch.setattr(operator_cli, "_store", lambda config: object())

    def fake_publish(received_api, store, table, campaign_code=None, dry_run=False):
        assert received_api is api
        assert api.enabled is False
        assert dry_run is True
        return {"status": "idle"}

    monkeypatch.setattr(operator_cli, "publish_next", fake_publish)

    code, result = operator_cli._run_publish(FakeConfig(), dry_run=True)

    assert code == 0
    assert result["status"] == "idle"
