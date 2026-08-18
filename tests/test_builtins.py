from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

from nodeclaw.core.tools.builtins import (
    calculator,
    delete_scheduled_task,
    get_current_time,
    get_system_model_info,
    list_scheduled_tasks,
    modify_scheduled_task,
    schedule_task,
)


RUNTIME = {"configurable": {"user_id": "user-1", "session_id": "session-1"}}


def test_get_current_time():
    result = get_current_time.invoke({})
    datetime.strptime(result.split(": ", 1)[1], "%Y-%m-%d %H:%M:%S")


def test_calculator_valid_and_invalid_expressions():
    assert "5" in calculator.invoke({"expression": "2 + 3"})
    assert "8" in calculator.invoke({"expression": "2 ** 3"})
    assert "计算出错" in calculator.invoke({"expression": "__import__('os')"})
    assert "计算出错" in calculator.invoke({"expression": "1 / 0"})


def test_get_system_model_info(monkeypatch):
    monkeypatch.setenv("DEFAULT_PROVIDER", "aliyun")
    monkeypatch.setenv("DEFAULT_MODEL", "glm-test")
    result = get_system_model_info.invoke({})
    assert "aliyun" in result
    assert "glm-test" in result


@patch("nodeclaw.core.tools.builtins.create_task")
def test_schedule_task_uses_authenticated_context(create_task_mock):
    target_time = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    create_task_mock.return_value = {"id": "task-1"}
    result = schedule_task.invoke(
        {"target_time": target_time, "description": "喝水", "repeat": "daily", "repeat_count": 3},
        config=RUNTIME,
    )
    assert "任务已成功加入队列" in result
    create_task_mock.assert_called_once_with(
        user_id="user-1",
        session_id="session-1",
        target_time=target_time,
        description="喝水",
        repeat="daily",
        repeat_count=3,
    )


def test_schedule_task_rejects_missing_user_context():
    result = schedule_task.invoke({"target_time": "2030-01-01 09:00:00", "description": "测试"})
    assert "缺少已认证用户上下文" in result


@patch("nodeclaw.core.tools.builtins.list_tasks")
def test_list_tasks_is_user_scoped(list_tasks_mock):
    list_tasks_mock.return_value = [{
        "id": "task-1", "target_time": "2030-01-01 09:00:00", "description": "上课"
    }]
    result = list_scheduled_tasks.invoke({}, config=RUNTIME)
    assert "上课" in result
    list_tasks_mock.assert_called_once_with("user-1")


@patch("nodeclaw.core.tools.builtins.delete_task", return_value=True)
def test_delete_task_is_user_scoped(delete_task_mock):
    result = delete_scheduled_task.invoke({"task_id": "task-1"}, config=RUNTIME)
    assert "已成功取消" in result
    delete_task_mock.assert_called_once_with("user-1", "task-1")


@patch("nodeclaw.core.tools.builtins.update_task")
def test_modify_task_is_user_scoped(update_task_mock):
    update_task_mock.return_value = {"id": "task-1"}
    result = modify_scheduled_task.invoke(
        {"task_id": "task-1", "new_description": "新内容"}, config=RUNTIME
    )
    assert "已成功更新" in result
    update_task_mock.assert_called_once_with(
        "user-1", "task-1", target_time=None, description="新内容"
    )
