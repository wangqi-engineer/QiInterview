"""岗位爬虫子模块。"""
from app.services.jobs.base import JobItem, JobSource
from app.services.jobs.tencent import TencentSource
from app.services.jobs.bytedance import ByteDanceSource
from app.services.jobs.alibaba import AlibabaSource


def all_sources() -> list[JobSource]:
    return [TencentSource(), ByteDanceSource(), AlibabaSource()]


__all__ = ["JobItem", "JobSource", "TencentSource", "ByteDanceSource", "AlibabaSource", "all_sources"]
