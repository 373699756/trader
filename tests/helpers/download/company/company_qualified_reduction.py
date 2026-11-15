"""Reduced source wording from CNInfo 1225583255, preserving actor boundaries."""

from trader.download.domain.company_evidence import CompanyDocument
from tests.helpers.download.company.company_evidence import _disclosure


TITLE = "关于持股5%以上股东、部分董事、高级管理人员减持股份的预披露公告"
STATEMENT = (
    "恒易有业计划自本公告发布之日起十五个交易日后的三个月内"
    "（即2026年10月26日至2027年1月25日），以集中竞价交易方式或大宗交易方式"
    "减持股份数量合计不超过4,456,020股（占公司总股本的1.4708%，"
    "占剔除公司回购专用账户股份后总股本的1.5120%）。"
)
HOLDER = (
    "华新绿源资源科技股份有限公司（以下简称“公司”或“华新科技”）股东"
    "海南恒易有业投资合伙企业（有限合伙）（以下简称“恒易有业”）为公司首次公开发行股票上市前股东，"
    "合计持有公司股份27,840,000股（占公司总股本比例为9.19%，"
    "占剔除公司回购专用账户股份后总股本比例为9.4438%），" + STATEMENT
)
DIRECTOR = (
    "公司董事会秘书兼财务总监李先生持有公司股份175,000股，（占公司总股本比例0.06%），"
    "计划自本公告发布之日起十五个交易日后的三个月内"
    "（即2026年10月26日至2027年1月25日）减持股份，数量不超过43,000股，占公司总股本的0.0142%。"
)
BODY = (
    "证券代码：600001\n关于持股5%以上股东、部分董事、高级管理人员拟减持股份的预披露公告\n"
    "公司持股5%以上股东海南恒易有业投资合伙企业（有限合伙）、公司董事高级管理人员保证真实。\n"
    "特别提示：\n1、" + HOLDER + "\n2、" + DIRECTOR + "\n公司于近日收到股东的减持计划告知函。\n"
    "其他合伙人间接持有股份，分别计划减持占公司总股本的0.2571%。"
)


def QualifiedReductionDocument(body=BODY):
    return CompanyDocument(_disclosure("qualified-plan", TITLE), body, True)
