"""IAM authentication for RDS Postgres (enabled with DB_IAM_AUTH=1).

Instead of a password in DATABASE_URL, each new connection gets a 15-minute
auth token signed with the Lambda's own IAM role. Signing happens locally (no
call to AWS), so it works from inside a VPC with no internet access.
"""
from sqlalchemy import event

# Replaceable in tests: (host, port, user) -> token
token_provider = None


def _rds_token_provider():
    import os

    import boto3  # provided by the Lambda runtime

    client = boto3.client("rds", region_name=os.environ.get("AWS_REGION"))  # set by Lambda

    def provide(host, port, user):
        return client.generate_db_auth_token(DBHostname=host, Port=port, DBUsername=user)

    return provide


def enable_iam_auth(engine):
    provide = token_provider or _rds_token_provider()
    url = engine.url

    @event.listens_for(engine, "do_connect")
    def _use_token(dialect, conn_rec, cargs, cparams):
        cparams["password"] = provide(url.host, url.port or 5432, url.username)
