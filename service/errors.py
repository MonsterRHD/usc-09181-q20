class WorkbenchError(Exception):
    """工作台领域错误基类。"""


class NotFound(WorkbenchError):
    pass


class ValidationError(WorkbenchError):
    pass


class IllegalTransition(WorkbenchError):
    pass


class DuplicateReceipt(WorkbenchError):
    """相同回执编号再次到达：幂等返回，不重复通知。"""


class ReceiptConflict(WorkbenchError):
    """相同回执编号但内容摘要不同，疑似编号冲突或伪造。"""


class OwnerChanged(WorkbenchError):
    """并发转派：案件当前责任人与预期不符。"""


class ImmutableReceipt(WorkbenchError):
    """银行原始回执不可修改，只能提交带原因的更正。"""
