"""Rate-limit identity (reliability hardening, 2026-10-22): behind one web key every signed-in
user must have their own bucket, anonymous traffic shares the key's bucket, forged headers
change nothing, and buckets refill."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from fpl_api.accounts import Principal
from fpl_api.security import Guard, TokenBucket, hash_key
from fpl_api.settings import Settings

WEB_KEY = "web-key-for-tests"
OPS_KEY = "operator-key-for-tests"


@dataclass
class FakeUsers:
    """Stands in for UserStore.principal: known session tokens map to users; counts lookups."""

    sessions: dict[str, Principal]
    lookups: int = 0
    seen: list[str] = field(default_factory=list)

    def principal(self, token: str) -> Principal | None:
        self.lookups += 1
        self.seen.append(token)
        return self.sessions.get(token)


def make_request(
    headers: dict[str, str], method: str = "POST", path: str = "/api/v1/squad"
) -> Request:
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("203.0.113.9", 1234),
    }
    return Request(scope)


def make_guard(per_minute: int = 3, trust_forwarded: bool = False) -> tuple[Guard, FakeUsers]:
    settings = Settings(
        require_api_key=True,
        web_api_keys_sha256=[hash_key(WEB_KEY)],
        api_keys_sha256=[hash_key(OPS_KEY)],
        rate_limit_per_minute=per_minute,
        expensive_rate_limit_per_minute=per_minute,
        trust_forwarded_for=trust_forwarded,
    )
    users = FakeUsers(
        {
            "tok-alice": Principal(kind="user", user_id="u-alice-0000000000", email="a@x.test"),
            "tok-bob": Principal(kind="user", user_id="u-bob-00000000000", email="b@x.test"),
        }
    )
    return Guard(settings, users), users  # type: ignore[arg-type]


def test_signed_in_users_behind_one_web_key_have_separate_buckets() -> None:
    guard, _ = make_guard(per_minute=3)
    alice = {"x-api-key": WEB_KEY, "authorization": "Bearer tok-alice"}
    bob = {"x-api-key": WEB_KEY, "authorization": "Bearer tok-bob"}
    for _ in range(3):
        guard.check_rate(make_request(alice))
    with pytest.raises(HTTPException) as exc:
        guard.check_rate(make_request(alice))
    assert exc.value.status_code == 429 and "Retry-After" in exc.value.headers
    # Alice's burst has not touched Bob
    for _ in range(3):
        guard.check_rate(make_request(bob))


def test_anonymous_traffic_shares_the_web_keys_bucket() -> None:
    guard, _ = make_guard(per_minute=3)
    anon = {"x-api-key": WEB_KEY}
    assert guard.client_id(make_request(anon)) == guard.client_id(make_request(anon))
    for _ in range(3):
        guard.check_rate(make_request(anon))
    with pytest.raises(HTTPException):
        guard.check_rate(make_request(anon))


def test_forged_headers_do_not_move_a_caller_to_a_fresh_bucket() -> None:
    guard, _ = make_guard(per_minute=2, trust_forwarded=False)
    # a client-supplied address is ignored unless the deployment trusts its TLS proxy
    a = guard.client_id(make_request({"x-api-key": WEB_KEY, "x-forwarded-for": "1.1.1.1"}))
    b = guard.client_id(make_request({"x-api-key": WEB_KEY, "x-forwarded-for": "2.2.2.2"}))
    assert a == b == "k:" + hash_key(WEB_KEY)[:16]
    # an invented session token is not a user: it falls back to the key's bucket
    forged = guard.client_id(
        make_request({"x-api-key": WEB_KEY, "authorization": "Bearer made-up"})
    )
    assert forged == a
    # and without any key the bucket is the connection address, whatever the headers say
    bare = guard.client_id(
        make_request({"x-forwarded-for": "9.9.9.9", "authorization": "Bearer made-up"})
    )
    assert bare == "ip:203.0.113.9"


def test_operator_key_keeps_its_own_bucket_and_identity() -> None:
    guard, _ = make_guard(per_minute=2)
    ops = make_request({"x-api-key": OPS_KEY})
    assert guard.client_id(ops) == "k:" + hash_key(OPS_KEY)[:16]
    assert guard.principal(ops).is_operator
    # a user session alongside the operator key is the user, not an operator
    both = make_request({"x-api-key": OPS_KEY, "authorization": "Bearer tok-alice"})
    assert guard.principal(both).kind == "user"


def test_session_is_resolved_once_per_request_and_reused_by_the_principal_check() -> None:
    guard, users = make_guard()
    req = make_request({"x-api-key": WEB_KEY, "authorization": "Bearer tok-alice"})
    guard.check_rate(req)
    p = guard.principal(req)
    assert p.kind == "user" and p.user_id and p.user_id.startswith("u-alice")
    assert users.lookups == 1


def test_invalid_session_still_fails_the_principal_check() -> None:
    guard, _ = make_guard()
    req = make_request({"x-api-key": WEB_KEY, "authorization": "Bearer made-up"})
    guard.check_rate(req)  # counted against the key's bucket, not refused here
    with pytest.raises(HTTPException) as exc:
        guard.principal(req)
    assert exc.value.status_code == 401


def test_abusive_burst_is_refused_and_the_bucket_refills() -> None:
    bucket = TokenBucket(rate_per_minute=60)
    ok = sum(1 for _ in range(100) if bucket.take("u:abuser", now=1000.0)[0])
    assert ok == 60
    refused, wait = bucket.take("u:abuser", now=1000.0)
    assert not refused and 0 < wait <= 1.0
    # a second later one token is back; a full minute later the whole allowance is
    assert bucket.take("u:abuser", now=1001.0)[0]
    assert sum(1 for _ in range(100) if bucket.take("u:abuser", now=1061.0)[0]) == 60
    # another caller is untouched throughout
    assert bucket.take("u:other", now=1000.0)[0]
