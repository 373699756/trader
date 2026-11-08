"""Explicit immutable current-industry inputs for isolated training fixtures."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from trader.download.domain.current_industry import CurrentIndustryMember, CurrentIndustryReference, CurrentIndustrySnapshot


def CurrentIndustrySnapshotFixture(
    codes: tuple[str, ...],
    *,
    sectors: tuple[tuple[str, str], ...] = (("J66", "货币金融服务"),),
    observed_at: datetime = datetime(2026, 11, 8, tzinfo=ZoneInfo("Asia/Shanghai")),
) -> CurrentIndustrySnapshot:
    classification = "中国上市公司协会上市公司行业分类标准"
    members = tuple(
        CurrentIndustryMember(
            code,
            CurrentIndustryReference(f"capco:{category}{name}", classification, observed_at.date(), "cninfo", "fixture"),
            name,
        )
        for index, code in enumerate(codes)
        for category, name in (sectors[index % len(sectors)],)
    )
    return CurrentIndustrySnapshot(
        members, (), observed_at, observed_at + timedelta(days=7), "cninfo", classification, "f" * 64,
    )
