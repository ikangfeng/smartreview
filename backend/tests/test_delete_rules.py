# 删除功能（历史任务 / 风控规则）——使用临时 SQLite，避免污染真实数据目录
import sqlite3

import pytest

from db import database, review_repository as repo


@pytest.fixture
def iso_db(tmp_path, monkeypatch):
    """把 database.connect 指向临时 DB 并执行 init_db。"""

    def _fake_connect():
        conn = sqlite3.connect(str(tmp_path / "test.db"), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    monkeypatch.setattr(database, "connect", _fake_connect)
    database.init_db()
    return tmp_path


def test_seed_rules_once(iso_db):
    """首次 init_db seed 3 条默认规则。"""
    rules = repo.list_rules()
    assert len(rules) == 3


def test_delete_rule_not_reseeded(iso_db):
    """删除规则后再次 init_db（模拟重启）不会复活默认规则。"""
    assert repo.delete_rule(1) is True
    assert repo.delete_rule(999) is False  # 不存在返回 False
    assert len(repo.list_rules()) == 2
    database.init_db()  # 模拟服务重启
    assert len(repo.list_rules()) == 2  # 不复活


def test_delete_task_cascade(iso_db):
    """删除任务时级联删除逐条结果，返回任务信息供上层清理磁盘文件。"""
    task_id = "t-delete-001"
    clauses = [{"clause_id": "1", "title": "第一条", "content": "违约金不超过30%"}]
    repo.create_task(task_id, "f123456789ab", "合同A.docx", "合同A",
                     "甲方", "乙方", "100万", clauses)
    repo.save_result(
        task_id, 1, clauses[0],
        {"risk_level": "高", "risk_description": "d", "law_basis": "",
         "suggestion": "s", "confidence": 0.95}, "rule")

    info = repo.delete_task(task_id)
    assert info is not None
    assert info["file_id"] == "f123456789ab"
    assert repo.get_task(task_id) is None  # 任务已删
    with database.get_conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM review_results WHERE task_id=?",
            (task_id,)).fetchone()["n"]
    assert n == 0  # 结果已级联删除

    # 不存在任务返回 None
    assert repo.delete_task("no-such-task") is None
