"""The default IAM token provider (api/db_auth.py), with boto3 stubbed out:
boto3 comes with the Lambda runtime and isn't a local dependency. The token's
use on a real connection is in tests/postgres/test_pg_deploy.py."""
import sys
import types

from api import db_auth


def test_tokens_are_signed_by_the_rds_client(monkeypatch):
    clients, requests = [], []

    class RdsClient:
        def generate_db_auth_token(self, **kwargs):
            requests.append(kwargs)
            return f"token-{len(requests)}"

    boto3 = types.ModuleType("boto3")
    boto3.client = lambda service, **kwargs: clients.append((service, kwargs)) or RdsClient()
    monkeypatch.setitem(sys.modules, "boto3", boto3)
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    provide = db_auth._rds_token_provider()
    assert provide("db.internal", 5432, "scheduler_app") == "token-1"
    assert provide("db.internal", 5432, "scheduler_app") == "token-2"  # a new one each time
    assert clients == [("rds", {"region_name": "us-east-1"})]  # one client, reused
    assert requests[0] == {"DBHostname": "db.internal", "Port": 5432, "DBUsername": "scheduler_app"}
