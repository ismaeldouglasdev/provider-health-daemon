from dashboard import DashboardHandler


def _handler_for(host):
    handler = object.__new__(DashboardHandler)
    handler.client_address = (host, 12345)
    return handler


def test_admin_client_allows_ipv4_loopback():
    assert DashboardHandler._is_local_admin_client(_handler_for("127.0.0.1")) is True


def test_admin_client_allows_ipv6_loopback():
    assert DashboardHandler._is_local_admin_client(_handler_for("::1")) is True


def test_admin_client_rejects_remote_ipv4():
    assert DashboardHandler._is_local_admin_client(_handler_for("192.168.1.10")) is False


def test_admin_client_rejects_malformed_address():
    assert DashboardHandler._is_local_admin_client(_handler_for("not-an-ip")) is False


def test_webhook_client_allows_loopback():
    assert DashboardHandler._is_local_admin_client(_handler_for("127.0.0.1")) is True


def test_webhook_client_rejects_remote_ipv4():
    assert DashboardHandler._is_local_admin_client(_handler_for("10.0.0.25")) is False
