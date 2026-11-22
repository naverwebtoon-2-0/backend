from tests.conftest import signup


def test_signup_author_with_invite_code(client):
    res = client.post(
        "/v1/auth/signup",
        json={
            "email": "A@Example.com",
            "password": "password123",
            "nickname": "작가A",
            "invite_code": "invite-123",
        },
    )
    assert res.status_code == 201
    user = res.json()["user"]
    assert user["role"] == "author"
    assert user["email"] == "a@example.com"
    assert "password" not in res.text


def test_signup_without_code_is_reader(client):
    res = client.post(
        "/v1/auth/signup", json={"email": "r@example.com", "password": "password123", "nickname": "독자"}
    )
    assert res.status_code == 201
    assert res.json()["user"]["role"] == "reader"


def test_signup_rejects_role_field_and_bad_inputs(client):
    base = {"email": "x@example.com", "password": "password123", "nickname": "x"}
    assert client.post("/v1/auth/signup", json={**base, "role": "author"}).status_code == 400
    assert client.post("/v1/auth/signup", json={**base, "password": "short"}).json()["error"]["code"] == (
        "VALIDATION_ERROR"
    )
    assert client.post("/v1/auth/signup", json={**base, "invite_code": "wrong"}).status_code == 400
    assert client.post("/v1/auth/signup", json={**base, "email": "not-an-email"}).status_code == 400


def test_signup_duplicate_email(client):
    body = {"email": "d@example.com", "password": "password123", "nickname": "d"}
    assert client.post("/v1/auth/signup", json=body).status_code == 201
    res = client.post("/v1/auth/signup", json={**body, "email": "D@example.com"})
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "EMAIL_ALREADY_EXISTS"


def test_login_and_me(client):
    headers = signup(client, "me@example.com")
    res = client.get("/v1/me", headers=headers)
    assert res.status_code == 200
    body = res.json()
    assert body["role"] == "author"
    assert body["settings"]["reader"]["autoplay"] is True


def test_login_wrong_password(client):
    signup(client, "w@example.com")
    res = client.post("/v1/auth/login", json={"email": "w@example.com", "password": "wrong-password"})
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "UNAUTHORIZED"


def test_login_rate_limit(client):
    signup(client, "rl@example.com")
    for _ in range(10):
        client.post("/v1/auth/login", json={"email": "rl@example.com", "password": "wrong-password"})
    res = client.post("/v1/auth/login", json={"email": "rl@example.com", "password": "password123"})
    assert res.status_code == 429
    assert "retry-after" in res.headers


def test_me_requires_token(client):
    assert client.get("/v1/me").status_code == 401
    res = client.get("/v1/me", headers={"Authorization": "Bearer garbage"})
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "UNAUTHORIZED"


def _login(client, email):
    return client.post("/v1/auth/login", json={"email": email, "password": "password123"}).json()


def test_refresh_rotates_and_detects_reuse(client):
    signup(client, "rt@example.com")
    tokens = _login(client, "rt@example.com")
    res = client.post("/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert res.status_code == 200
    rotated = res.json()
    assert rotated["refresh_token"] != tokens["refresh_token"]
    assert rotated["expires_in"] == 3600
    # 이전 토큰 재사용 → 401, 그리고 새 토큰까지 모두 폐기된다
    assert client.post("/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code == 401
    assert (
        client.post("/v1/auth/refresh", json={"refresh_token": rotated["refresh_token"]}).status_code == 401
    )


def test_refresh_invalid(client):
    assert client.post("/v1/auth/refresh", json={"refresh_token": "nope"}).status_code == 401


def test_logout_revokes(client):
    signup(client, "lo@example.com")
    tokens = _login(client, "lo@example.com")
    assert client.post("/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]}).status_code == 204
    assert client.post("/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code == 401
    assert client.post("/v1/auth/logout", json={}).status_code == 400
