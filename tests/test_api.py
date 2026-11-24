"""HTTP API 测试：/quotas、/status、/transfers。"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.service import TransferService

A, B = "groupA", "groupB"


@pytest.fixture
def client(tmp_path):
    cg = tmp_path / "cgroup"
    for name, q in ((A, 100000), (B, 50000)):
        p = cg / name / "cpu.max"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"{q} 100000\n")
    svc = TransferService(
        {A: cg / A / "cpu.max", B: cg / B / "cpu.max"}, tmp_path / "state", 20000
    )
    app = create_app(svc)
    with TestClient(app) as c:  # 进入上下文即执行 lifespan（含启动恢复）
        yield c, svc
    svc.close()


def test_quotas_and_status(client):
    c, svc = client
    r = c.get("/quotas")
    assert r.status_code == 200
    body = r.json()
    assert body[A]["quota"] == "100000"
    assert body[B]["quota"] == "50000"
    assert body[A]["period"] == body[B]["period"] == 100000

    r = c.get("/status")
    assert r.status_code == 200
    assert r.json()["recovered"] is True


def test_transfer_happy_path(client):
    c, svc = client
    r = c.post("/transfers", json={"donor": B, "amount": 20000})
    assert r.status_code == 200
    body = r.json()
    assert body["result"] == "success"
    assert body["receiver"] == A

    quotas = c.get("/quotas").json()
    assert quotas[A]["quota"] == "120000"
    assert quotas[B]["quota"] == "30000"

    status = c.get("/status").json()
    assert status["last_result"]["result"] == "success"
    assert status["last_result"]["transfer_id"] == body["transfer_id"]


def test_transfer_validation_errors(client):
    c, _ = client
    assert c.post("/transfers", json={"donor": "groupC", "amount": 1}).status_code == 400
    assert c.post("/transfers", json={"donor": B, "amount": 0}).status_code == 422
    # 低于下限：50000 - 30001 = 19999 < 20000
    r = c.post("/transfers", json={"donor": B, "amount": 30001})
    assert r.status_code == 400
    assert "下限" in r.json()["detail"]


def test_transfer_rejected_when_recovery_failed(tmp_path):
    cg = tmp_path / "cgroup"
    for name, q in ((A, 100000), (B, 50000)):
        p = cg / name / "cpu.max"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"{q} 100000\n")
    state = tmp_path / "state"
    state.mkdir()
    (state / "transfer_journal.json").write_text("损坏的日志")
    svc = TransferService({A: cg / A / "cpu.max", B: cg / B / "cpu.max"}, state, 20000)
    app = create_app(svc)
    with TestClient(app, raise_server_exceptions=False) as c:
        # 启动恢复失败 → recovered=False → 拒绝新转移
        r = c.post("/transfers", json={"donor": B, "amount": 1000})
        assert r.status_code == 503
        status = c.get("/status").json()
        assert status["recovered"] is False
        assert status["last_result"]["result"] == "failed"
        # 查询配额不受恢复状态影响
        assert c.get("/quotas").status_code == 200
    svc.close()
