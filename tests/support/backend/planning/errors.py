"""切片 3 规划模块受控错误。

供应商/内部原始异常一律映射为受控错误码（注册于 logging_audit.contracts），
不透出原始文本；错误码同时进入任务事件与用户可见提示。
"""

from __future__ import annotations


class PlanningError(Exception):
    status = 400

    def __init__(self, code: str, message: str = "", status: int | None = None) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        if status is not None:
            self.status = status


class PlanningDuplicate(PlanningError):
    def __init__(self, detail: dict):
        super().__init__("PLANNING_DUPLICATE", "已有相同课程", status=409)
        self.detail = detail


def error(code: str, message: str = "", status: int | None = None) -> PlanningError:
    return PlanningError(code, message, status)


class WaitStateSignal(Exception):
    """业务等待态（awaiting_scope_clarification / awaiting_search_decision）。

    不计为失败：worker 捕获后记录任务事件（partial + 受控错误码）并保存
    wait_state，等待用户决策后恢复。
    """

    def __init__(self, wait_state: str, *, question: str = "", detail: str = "") -> None:
        super().__init__(wait_state)
        self.wait_state = wait_state
        self.question = question
        self.detail = detail

    @property
    def error_code(self) -> str:
        return self.wait_state.upper()
