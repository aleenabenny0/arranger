"""The three file stores keep one contract, and S3 requests are signed correctly.

The S3 store has never talked to a real bucket from this test suite. What is
tested here is the request signing, against the worked example in AWS's own
documentation, and the store's behaviour against a fake HTTP transport that
acts like a bucket. `python -m arranger_api.artifacts --check` is the tool for
checking a real one.
"""

from __future__ import annotations

import datetime as dt

import pytest

pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from arranger_api.artifacts import (  # noqa: E402
    ArtifactError,
    ArtifactNotFound,
    DatabaseArtifactStore,
    LocalArtifactStore,
    S3ArtifactStore,
    new_storage_key,
    safe_filename,
    sigv4_headers,
)
from arranger_api.storage import Database  # noqa: E402

EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_signature_matches_the_worked_example_in_the_aws_documentation():
    # "Signature Calculations for the Authorization Header", example "GET Object":
    # the first ten bytes of /test.txt in examplebucket, 24 May 2013, us-east-1.
    headers = sigv4_headers(
        method="GET", host="examplebucket.s3.amazonaws.com", path="/test.txt", region="us-east-1",
        access_key="AKIAIOSFODNN7EXAMPLE", secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        payload_sha256=EMPTY_SHA256, now=dt.datetime(2013, 5, 24, 0, 0, 0, tzinfo=dt.timezone.utc),
        extra_headers={"Range": "bytes=0-9"},
    )
    assert headers["authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )
    assert headers["x-amz-date"] == "20130524T000000Z"
    assert "host" not in headers, "the HTTP client sets Host itself"


def test_a_different_secret_gives_a_different_signature():
    common = dict(method="GET", host="h.example", path="/b/k", region="auto", access_key="AK",
                  payload_sha256=EMPTY_SHA256, now=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
    one = sigv4_headers(secret_key="one", **common)["authorization"]
    two = sigv4_headers(secret_key="two", **common)["authorization"]
    assert one != two
    assert "one" not in one and "two" not in two, "the secret never appears in a header"


class FakeBucket:
    """Enough of S3 to store objects: path-style PUT, GET, HEAD and DELETE."""

    def __init__(self, fail_with: int | None = None):
        self.objects: dict[str, bytes] = {}
        self.requests: list[httpx.Request] = []
        self.fail_with = fail_with

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_with:
            return httpx.Response(self.fail_with, text="<Error><Code>InternalError</Code></Error>")
        assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKTEST/")
        assert request.headers["x-amz-content-sha256"]
        key = request.url.path
        if request.method == "PUT":
            self.objects[key] = request.content
            return httpx.Response(200)
        if request.method == "DELETE":
            self.objects.pop(key, None)
            return httpx.Response(204)
        if key not in self.objects:
            return httpx.Response(404)
        return httpx.Response(200, content=b"" if request.method == "HEAD" else self.objects[key])


def s3_store(bucket: FakeBucket) -> S3ArtifactStore:
    return S3ArtifactStore(endpoint="https://objects.example", bucket="arranger-files", region="auto",
                           access_key="AKTEST", secret_key="not-a-real-secret",
                           client=httpx.Client(transport=httpx.MockTransport(bucket)))


@pytest.fixture(params=["local", "database", "s3"])
def store(request, tmp_path):
    if request.param == "local":
        yield LocalArtifactStore(tmp_path / "files")
    elif request.param == "database":
        from arranger_api.storage import init_db

        database = Database(sqlite_path=str(tmp_path / "files.db"))
        with database.connection() as conn:
            init_db(conn)
        try:
            yield DatabaseArtifactStore(database)
        finally:
            database.close()
    else:
        yield s3_store(FakeBucket())


def test_every_store_keeps_the_same_contract(store):
    key = new_storage_key("user-1", "midi")
    data = bytes(range(256)) * 40
    assert store.healthy()
    assert not store.exists(key)
    with pytest.raises(ArtifactNotFound):
        store.get(key)

    store.put(key, data, "audio/midi")
    assert store.exists(key)
    assert store.get(key) == data

    store.put(key, b"replaced", "audio/midi")          # writing again replaces, never appends
    assert store.get(key) == b"replaced"

    store.delete(key)
    assert not store.exists(key)
    store.delete(key)                                   # deleting what is gone is not an error


@pytest.mark.parametrize("key", ["../etc/passwd", "a/../../b", "/absolute", "a//b", "a/b\x00c", "", "a\\b"])
def test_every_store_refuses_a_key_that_is_not_one_it_made(store, key):
    with pytest.raises(ArtifactError):
        store.put(key, b"x", "application/octet-stream")
    with pytest.raises(ArtifactError):
        store.get(key)


def test_s3_uses_path_style_urls_and_sends_the_content_type():
    bucket = FakeBucket()
    store = s3_store(bucket)
    key = new_storage_key("user-1", "pdf")
    store.put(key, b"%PDF-1.7", "application/pdf")
    sent = bucket.requests[-1]
    assert str(sent.url) == f"https://objects.example/arranger-files/{key}"
    assert sent.headers["content-type"] == "application/pdf"
    assert "content-type" in sent.headers["authorization"].split("SignedHeaders=")[1]


def test_s3_failures_are_errors_without_the_provider_body():
    store = s3_store(FakeBucket(fail_with=500))
    with pytest.raises(ArtifactError) as caught:
        store.put(new_storage_key("u", "midi"), b"x", "audio/midi")
    assert "500" in str(caught.value) and "InternalError" not in str(caught.value)
    assert store.healthy() is False

    def unreachable(request):
        raise httpx.ConnectError("no route to host objects.example")

    offline = S3ArtifactStore(endpoint="https://objects.example", bucket="arranger-files", region="auto",
                              access_key="AKTEST", secret_key="s", client=httpx.Client(transport=httpx.MockTransport(unreachable)))
    assert offline.healthy() is False
    with pytest.raises(ArtifactError) as caught:
        offline.get(new_storage_key("u", "midi"))
    assert "objects.example" not in str(caught.value), "the endpoint is configuration, not something to show a user"


@pytest.mark.parametrize("bucket", ["UPPER", "a", "has_underscore", "-leading", "trailing-", "x" * 64])
def test_s3_refuses_bucket_names_that_could_change_the_request_path(bucket):
    with pytest.raises(ArtifactError):
        S3ArtifactStore(endpoint="https://objects.example", bucket=bucket, region="auto", access_key="a", secret_key="b")


def test_download_names_are_made_safe():
    assert safe_filename("Für Elise (easy).mid", "piece.mid").endswith(".mid")
    for hostile in ('a"b.mid', "a\r\nSet-Cookie: x=1.mid", "../../etc/passwd", "..", ""):
        cleaned = safe_filename(hostile, "piece.mid")
        assert cleaned and not set(cleaned) & set('"\r\n/\\'), cleaned
        assert cleaned not in {".", ".."}
