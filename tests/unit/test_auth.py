# tests/unit/test_auth.py
async def test_auth_valid_secret(mock_auth_service):
    mgr = AuthManager(endpoint=mock_auth_service.addr, use_tls=False)
    result = await mgr.init("valid_secret")
    assert result is None
    assert mgr.jwt == "test_jwt"
    assert mgr.primary_account_id == "ACC001"
    await mgr.shutdown()