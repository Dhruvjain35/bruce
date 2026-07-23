"""_owner_conn URL parsing — must handle the Cloud SQL unix-socket form (the OAuth-callback 500 cause)."""

from bruce_engine import retention


def test_owner_conn_kwargs_cloud_sql_socket():
    # Cloud Run: the instance is a unix socket in ?host=/cloudsql/INSTANCE, NOT a TCP host
    kw = retention._owner_conn_kwargs(
        "postgresql+asyncpg://owner:pw@/postgres?host=/cloudsql/proj:us-central1:inst")
    assert kw["host"] == "/cloudsql/proj:us-central1:inst"     # socket dir, not 127.0.0.1
    assert kw["database"] == "postgres" and kw["user"] == "owner"


def test_owner_conn_kwargs_tcp_host():
    kw = retention._owner_conn_kwargs("postgresql+asyncpg://owner:pw@10.1.2.3:5432/postgres")
    assert kw["host"] == "10.1.2.3" and kw["port"] == 5432 and kw["database"] == "postgres"


def test_owner_conn_kwargs_local():
    kw = retention._owner_conn_kwargs("postgresql+asyncpg://dhruvjain@localhost:5432/postgres")
    assert kw["host"] == "localhost" and kw["port"] == 5432
