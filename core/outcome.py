class JobSkipped(RuntimeError):
    """能力不支持、已完成或无进展；结束当前项，继续队列。"""


class PartialCompletion(JobSkipped):
    """常规阶段已完成，但满分目标未达到。"""
