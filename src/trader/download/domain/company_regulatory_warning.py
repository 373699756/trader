"""Bind a regional CSRC warning decision to its issuer and current disclosure."""

import re

from trader.download.domain.company_evidence import CompanyDocument


_TITLE = re.compile(r"关于(?:公司(?:及相关人员)?)?收到(?P<region>[\u4e00-\u9fff]{2,4})证监局警示函的公告")
_HEADER = re.compile(r"证券代码[：:](\d{6})证券简称[：:]([^：:]{2,12}?)公告编号[：:]")
_SIGNATURE = re.compile(
    r"特此公告。[\s]*(?P<issuer>[\u4e00-\u9fffA-Za-z0-9（）()·]{2,42}股份有限公司)董事会"
    r"(?P<year>20\d{2})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日(?:第\d+页共\d+页)?$"
)
_RECIPIENTS = re.compile(r"[、，,]")
_DECISION = re.compile(r"[，。；：]我局决定对([^。；：]{1,120}?)采取出具警示函的行政监管措施")
_CONFLICT = re.compile(
    r"(?:尚未|并未|没有|未曾|未)(?:收到|对本公司出具)该?警示函|(?:不涉及|不包括)本公司"
    r"|(?:撤销|撤回|撤消|取消|失效|不予采取).{0,12}(?:警示函|监管措施|决定)"
    r"|(?:警示函|监管措施|决定).{0,8}(?:已撤销|已撤回|已失效)"
    r"|(?:引用|援引|引述)(?:历史|以往|过往|此前)|(?:历史|以往|过往)案例"
)


def IsCompanyRegulatorWarningNotice(title: str) -> bool:
    return _TITLE.fullmatch("".join(title.split())) is not None


def ReadCompanyRegulatorWarningDecision(document: CompanyDocument) -> str | None:
    title = _TITLE.fullmatch("".join(document.disclosure.title.split()))
    text = "".join(document.text.split())
    if title is None or not document.checked or _CONFLICT.search(text):
        return None
    header = _HEADER.findall(text[:500])
    signature = _SIGNATURE.search(text)
    if len(header) != 1 or signature is None or document.disclosure.codes != (header[0][0],):
        return None
    published = document.disclosure.published_at.date()
    if (published.year, published.month, published.day) != tuple(
        int(signature[name]) for name in ("year", "month", "day")
    ):
        return None
    issuer = signature["issuer"]
    receipt = re.compile(
        r"[。；]" + re.escape(issuer) + r"(?:（以下简称[^（）]{1,55}）)?(?:于)?(?:近日|今日)收到"
        r"中国证券监督管理委员会" + re.escape(title["region"]) + r"监管局(?:下发|出具)的"
        r"《关于对(?P<targets>[^《》。；]{1,130})采取出具警示函措施的决定》[（(]"
        r"[\[［〔](?P<year>20\d{2})[\]］〕](?P<number>\d{1,6})号[）)]"
    )
    decisions = list(receipt.finditer(text))
    if len(decisions) != 1 or issuer not in _RECIPIENTS.split(decisions[0]["targets"]):
        return None
    operative = _DECISION.findall(text[decisions[0].end():signature.start()])
    if len(operative) != 1:
        return None
    recipients = _RECIPIENTS.split(operative[0])
    if operative[0] != "你们" and issuer not in recipients and header[0][1] not in recipients:
        return None
    return f"{title['region']}证监局〔{decisions[0]['year']}〕{decisions[0]['number']}号"
