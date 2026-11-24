"""周期切换的 HTTP API 测试：/quotas 实际周期、/status 切换结果、切换后划转。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.service import TransferService

A, B = "groupA", "groupB"
PERIOD = 100000
SHORT = 50000


def make_client(tmp_path, target_period=None):
    cg = tmp_path / "cgroup"
    for name, q in ((A, 100000), (B, 50000)):
        p = cg / name / "cpu.max"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"{q} {PERIOD}\n")
    svc = TransferService(
        {A: cg / A / "cpu.max", B: cg / B / "cpu.max"},
        tmp_path / "state",
        20000,
        target_period=target_period,
    )
    app = create_app(svc)
    client = TestClient(app)
    client.__enter__()
    return client, svc


def test_quotas_show_actual_period_and_baseline(tmp_path):
    c, svc = make_client(tmp_path, target_period=SHORT)
    try:
        body = c.get("/quotas").json()
        assert body[A]["quota"] == "50000"
        assert body[A]["period"] == SHORT
        assert body[B]["quota"] == "25000"
        assert body[B]["period"] == SHORT
        assert body["periods"] == {
            "baseline_period": PERIOD,
            "active_period": SHORT,
        }
    finally:
        c.__exit__(None, None, None)
        svc.close()


def test_status_reports_switch_result(tmp_path):
    c, svc = make_client(tmp_path, target_period=SHORT)
    try:
        status = c.get("/status").json()
        ps = status["period_switch"]
        assert ps["configured_target_period"] == SHORT
        assert ps["baseline_period"] == PERIOD
        assert ps["active_period"] == SHORT
        assert ps["switched"] is True
        assert status["last_result"]["action"] == "period_switch"
        assert status["last_result"]["result"] == "switched"
        assert status["min_quota"] == 20000
        assert status["min_quota_period"] == PERIOD
    finally:
        c.__exit__(None, None, None)
        svc.close()


def test_status_without_switch_config(tmp_path):
    c, svc = make_client(tmp_path)
    try:
        status = c.get("/status").json()
        ps = status["period_switch"]
        assert ps["configured_target_period"] is None
        assert ps["switched"] is False
        assert ps["active_period"] == PERIOD
        assert status["last_result"] is None
    finally:
        c.__exit__(None, None, None)
        svc.close()


def test_transfer_after_switch_keeps_baseline_amount_semantics(tmp_path):
    c, svc = make_client(tmp_path, target_period=SHORT)
    try:
        r = c.post("/transfers", json={"donor": B, "amount": 20000})
        assert r.status_code == 200
        body = r.json()
        # amount 仍是基线单位；实际写入 delta 按短周期折算
        assert body["amount"] == 20000
        assert body["amount_period"] == PERIOD
        assert body["applied_delta"] == 10000
        assert body["active_period"] == SHORT
        assert body["post"][A] == "60000 50000"
        assert body["post"][B] == "15000 50000"

        quotas = c.get("/quotas").json()
        assert quotas[A]["period"] == SHORT
    finally:
        c.__exit__(None, None, None)
        svc.close()


def test_inexact_transfer_amount_rejected_over_http(tmp_path):
    c, svc = make_client(tmp_path, target_period=30000)
    try:
        # 101 基线单位 * 30000/100000 = 30.3，非整数
        r = c.post("/transfers", json={"donor": B, "amount": 101})
        assert r.status_code == 400
        assert "无法精确换算" in r.json()["detail"]
    finally:
        c.__exit__(None, None, None)
        svc.close()


def test_inexact_switch_rejected_and_status_shows_it(tmp_path):
    cg = tmp_path / "cgroup"
    for name, q in ((A, 100000), (B, 50001)):
        p = cg / name / "cpu.max"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"{q} {PERIOD}\n")
    svc = TransferService(
        {A: cg / A / "cpu.max", B: cg / B / "cpu.max"},
        tmp_path / "state",
        20000,
        target_period=30000,
    )
    app = create_app(svc)
    with TestClient(app) as c:
        status = c.get("/status").json()
        assert status["period_switch"]["switched"] is False
        assert status["last_result"]["result"] == "rejected"
        quotas = c.get("/quotas").json()
        assert quotas[B]["period"] == PERIOD  # 仍按旧周期
        # 服务仍可用
        r = c.post("/transfers", json={"donor": B, "amount": 1000})
        assert r.status_code == 200
    svc.close()


def test_invalid_period_env_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("GROUP_A_CPU_MAX", str(tmp_path / "a"))
    monkeypatch.setenv("GROUP_B_CPU_MAX", str(tmp_path / "b"))
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    from app.main import build_service_from_env

    for bad in ("abc", "0", "-5"):
        monkeypatch.setenv("CPU_PERIOD_USEC", bad)
        with pytest.raises(ValueError, match="CPU_PERIOD_USEC"):
            build_service_from_env()

    monkeypatch.delenv("CPU_PERIOD_USEC", raising=False)
    assert build_service_from_env().target_period is None
