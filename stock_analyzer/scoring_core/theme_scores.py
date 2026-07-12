from __future__ import annotations

from typing import List, Tuple

import pandas as pd

from ..normalization import coerce_number
from .theme_constants import CHOKEPOINT_CHAIN, TECH_THEMES


__all__ = [
    "CHOKEPOINT_INDUSTRY_LEADERS",
    "CHOKEPOINT_KEYWORDS",
    "SERENITY_REFERENCES",
    "_chain_segment",
    "_chokepoint_score",
    "_tech_theme_score",
]


SERENITY_REFERENCES = (
    {
        "repo": "Serenity / chokepoint 投资方法论",
        "url": "https://github.com/14H034160212/AlphaTrader",
        "adopted": "卡脖子/瓶颈投资：上溯供应链，挖掘供给最紧、尚未被重定价的环节",
    },
    {
        "repo": "wbh604/UZI-Skill (Group I)",
        "url": "https://github.com/wbh604/UZI-Skill",
        "adopted": "结构化证据 + 数据覆盖自检门控 + 共识极化拉伸",
    },
)


CHOKEPOINT_KEYWORDS = (
    "材料", "衬底", "封装", "载板", "光刻胶", "光模块", "光芯片", "硅片",
    "靶材", "电子特气", "前驱体", "掩膜", "EDA", "IP核", "刻蚀", "量测", "零部件",
    "元件", "晶圆", "陶瓷", "薄膜", "磁材", "永磁", "稀土", "精密",
    "玻璃基板", "玻璃载板", "玻璃通孔", "TGV", "玻璃基", "封装玻璃",
    "工业母机", "数控", "机床", "五轴", "伺服", "减速器", "谐波", "丝杠",
    "液冷", "温控", "电源", "UPS", "CPO", "传感器", "MEMS", "科学仪器",
    "操作系统", "数据库", "中间件", "信创", "航发", "航空", "航天", "钛合金",
    "碳纤维", "复材", "超导", "医疗设备", "高端医疗", "测量仪器",
    "卫星互联网", "低轨卫星", "低轨星座", "中国星网", "国网星座", "千帆",
    "G60星链", "卫星通信", "卫星载荷", "星载", "相控阵", "卫星终端",
    "终端天线", "地面站", "射频", "微波", "空间信息",
    "光刻机", "光刻物镜", "投影物镜", "准分子", "精密光学", "光学元件",
    "AI芯片", "GPU", "GPGPU", "CPU", "DPU", "算力芯片", "国产算力",
    "Chiplet", "HBM", "先进封装", "高速互连", "高频高速", "高速PCB",
    "铜缆", "连接器", "工业软件", "CAE", "CAD", "PLM", "MES", "DCS",
    "PLC", "伺服驱动", "运动控制", "轴承", "导轨", "滚珠丝杠", "液压",
    "密封", "阀门", "泵", "SiC", "碳化硅", "GaN", "氮化镓", "功率半导体",
    "IGBT", "MLCC", "电容", "薄膜电容", "陶瓷电容", "科研试剂", "生物试剂",
    "培养基", "工业酶", "合成生物", "生物育种", "种业", "转基因", "分离膜",
    "反渗透膜", "催化剂", "吸附树脂", "质子交换膜",
)


CHOKEPOINT_INDUSTRY_LEADERS = {
    "先进光刻/精密光学": (
        {"code": "688502", "name": "茂莱光学"},
        {"code": "002222", "name": "福晶科技"},
        {"code": "603297", "name": "永新光学"},
        {"code": "002338", "name": "奥普光电"},
        {"code": "688127", "name": "蓝特光学"},
        {"code": "688195", "name": "腾景科技"},
    ),
    "半导体设备": (
        {"code": "002371", "name": "北方华创"},
        {"code": "688012", "name": "中微公司"},
        {"code": "688072", "name": "拓荆科技"},
        {"code": "688120", "name": "华海清科"},
    ),
    "半导体材料": (
        {"code": "688126", "name": "沪硅产业"},
        {"code": "688019", "name": "安集科技"},
        {"code": "300346", "name": "南大光电"},
        {"code": "300666", "name": "江丰电子"},
    ),
    "国产算力芯片/IP": (
        {"code": "688256", "name": "寒武纪"},
        {"code": "688041", "name": "海光信息"},
        {"code": "688047", "name": "龙芯中科"},
        {"code": "300474", "name": "景嘉微"},
        {"code": "688385", "name": "复旦微电"},
        {"code": "002049", "name": "紫光国微"},
    ),
    "先进封装/HBM": (
        {"code": "600584", "name": "长电科技"},
        {"code": "002156", "name": "通富微电"},
        {"code": "002185", "name": "华天科技"},
        {"code": "002916", "name": "深南电路"},
        {"code": "002436", "name": "兴森科技"},
        {"code": "300476", "name": "胜宏科技"},
    ),
    "封装/载板": (
        {"code": "600584", "name": "长电科技"},
        {"code": "002156", "name": "通富微电"},
        {"code": "002916", "name": "深南电路"},
        {"code": "002436", "name": "兴森科技"},
    ),
    "玻璃基板/TGV": (
        {"code": "603773", "name": "沃格光电"},
        {"code": "300162", "name": "雷曼光电"},
        {"code": "688170", "name": "德龙激光"},
        {"code": "300554", "name": "三超新材"},
        {"code": "600552", "name": "凯盛科技"},
    ),
    "EDA/IP": (
        {"code": "301269", "name": "华大九天"},
        {"code": "688206", "name": "概伦电子"},
        {"code": "301095", "name": "广立微"},
    ),
    "工业软件/CAE": (
        {"code": "688083", "name": "中望软件"},
        {"code": "688507", "name": "索辰科技"},
        {"code": "603859", "name": "能科科技"},
        {"code": "300687", "name": "赛意信息"},
        {"code": "300378", "name": "鼎捷数智"},
        {"code": "600845", "name": "宝信软件"},
    ),
    "光器件": (
        {"code": "300308", "name": "中际旭创"},
        {"code": "300502", "name": "新易盛"},
        {"code": "300394", "name": "天孚通信"},
        {"code": "002281", "name": "光迅科技"},
    ),
    "AI算力液冷/电源": (
        {"code": "002837", "name": "英维克"},
        {"code": "300499", "name": "高澜股份"},
        {"code": "002335", "name": "科华数据"},
        {"code": "002518", "name": "科士达"},
    ),
    "高速互连/高频高速PCB": (
        {"code": "002463", "name": "沪电股份"},
        {"code": "002916", "name": "深南电路"},
        {"code": "600183", "name": "生益科技"},
        {"code": "300476", "name": "胜宏科技"},
        {"code": "688629", "name": "华丰科技"},
        {"code": "688800", "name": "瑞可达"},
        {"code": "300563", "name": "神宇股份"},
    ),
    "工业母机/高端数控": (
        {"code": "688305", "name": "科德数控"},
        {"code": "300161", "name": "华中数控"},
        {"code": "000837", "name": "秦川机床"},
        {"code": "601882", "name": "海天精工"},
        {"code": "688558", "name": "国盛智科"},
    ),
    "高端轴承/丝杠导轨": (
        {"code": "603667", "name": "五洲新春"},
        {"code": "002046", "name": "国机精工"},
        {"code": "300580", "name": "贝斯特"},
        {"code": "300718", "name": "长盛轴承"},
        {"code": "300850", "name": "新强联"},
        {"code": "000837", "name": "秦川机床"},
    ),
    "机器人核心零部件": (
        {"code": "688017", "name": "绿的谐波"},
        {"code": "300124", "name": "汇川技术"},
        {"code": "002472", "name": "双环传动"},
        {"code": "002050", "name": "三花智控"},
        {"code": "603728", "name": "鸣志电器"},
    ),
    "工业控制/PLC": (
        {"code": "688777", "name": "中控技术"},
        {"code": "300124", "name": "汇川技术"},
        {"code": "600845", "name": "宝信软件"},
        {"code": "603416", "name": "信捷电气"},
        {"code": "002851", "name": "麦格米特"},
        {"code": "002979", "name": "雷赛智能"},
    ),
    "SiC/GaN功率半导体": (
        {"code": "688234", "name": "天岳先进"},
        {"code": "600703", "name": "三安光电"},
        {"code": "603290", "name": "斯达半导"},
        {"code": "688261", "name": "东微半导"},
        {"code": "605111", "name": "新洁能"},
        {"code": "300373", "name": "扬杰科技"},
    ),
    "被动元件/高端电容": (
        {"code": "300408", "name": "三环集团"},
        {"code": "000636", "name": "风华高科"},
        {"code": "600563", "name": "法拉电子"},
        {"code": "002484", "name": "江海股份"},
        {"code": "300726", "name": "宏达电子"},
        {"code": "603678", "name": "火炬电子"},
    ),
    "高端材料": (
        {"code": "300285", "name": "国瓷材料"},
        {"code": "300777", "name": "中简科技"},
        {"code": "300699", "name": "光威复材"},
        {"code": "688295", "name": "中复神鹰"},
        {"code": "600206", "name": "有研新材"},
    ),
    "稀土/关键金属": (
        {"code": "600111", "name": "北方稀土"},
        {"code": "000831", "name": "中国稀土"},
        {"code": "300748", "name": "金力永磁"},
        {"code": "000970", "name": "中科三环"},
        {"code": "600549", "name": "厦门钨业"},
    ),
    "基础软件/信创": (
        {"code": "688111", "name": "金山办公"},
        {"code": "600536", "name": "中国软件"},
        {"code": "688058", "name": "宝兰德"},
        {"code": "002368", "name": "太极股份"},
    ),
    "科学仪器/高端医疗设备": (
        {"code": "300760", "name": "迈瑞医疗"},
        {"code": "688271", "name": "联影医疗"},
        {"code": "688114", "name": "华大智造"},
        {"code": "300203", "name": "聚光科技"},
        {"code": "688139", "name": "海尔生物"},
    ),
    "科研试剂/生物制造": (
        {"code": "688105", "name": "诺唯赞"},
        {"code": "688179", "name": "阿拉丁"},
        {"code": "688133", "name": "泰坦科技"},
        {"code": "688293", "name": "奥浦迈"},
        {"code": "301080", "name": "百普赛斯"},
        {"code": "301047", "name": "义翘神州"},
    ),
    "种业/生物育种": (
        {"code": "000998", "name": "隆平高科"},
        {"code": "002385", "name": "大北农"},
        {"code": "002041", "name": "登海种业"},
        {"code": "300087", "name": "荃银高科"},
        {"code": "000713", "name": "丰乐种业"},
        {"code": "300189", "name": "神农种业"},
    ),
    "高端膜材料/催化剂": (
        {"code": "002643", "name": "万润股份"},
        {"code": "300487", "name": "蓝晓科技"},
        {"code": "300631", "name": "久吾高科"},
        {"code": "688101", "name": "三达膜"},
        {"code": "601208", "name": "东材科技"},
        {"code": "000920", "name": "沃顿科技"},
    ),
    "卫星互联网/低轨星座": (
        {"code": "600118", "name": "中国卫星"},
        {"code": "601698", "name": "中国卫通"},
        {"code": "300045", "name": "华力创通"},
        {"code": "002465", "name": "海格通信"},
        {"code": "002151", "name": "北斗星通"},
        {"code": "300101", "name": "振芯科技"},
    ),
    "航空航天材料/零部件": (
        {"code": "600893", "name": "航发动力"},
        {"code": "600765", "name": "中航重机"},
        {"code": "600862", "name": "中航高科"},
        {"code": "688122", "name": "西部超导"},
        {"code": "688333", "name": "铂力特"},
    ),
    "高端阀门/密封泵": (
        {"code": "300470", "name": "中密控股"},
        {"code": "002438", "name": "江苏神通"},
        {"code": "603699", "name": "纽威股份"},
        {"code": "603308", "name": "应流股份"},
        {"code": "603100", "name": "川仪股份"},
        {"code": "300838", "name": "浙江力诺"},
    ),
    "精密零部件": (
        {"code": "300007", "name": "汉威科技"},
        {"code": "603662", "name": "柯力传感"},
        {"code": "688322", "name": "奥比中光"},
        {"code": "688539", "name": "高华科技"},
    ),
}


def _chain_segment(hits: List[str]) -> str:
    """把卡脖子命中词归类到产业链环节名；无法归类则返回'其他上游'。"""
    for kw in hits:
        for node in CHOKEPOINT_CHAIN:
            if any(k in kw or kw in k for k in node["keywords"]):
                return node["segment"]
    return "其他上游"


def _chokepoint_score(row: pd.Series) -> Tuple[float, List[str]]:
    """A6：卡脖子/上游环节倾斜（Serenity chokepoint 方法论）。

    命中上游/元件类关键词，且“供给紧但尚未被重定价”（近期涨幅温和）时给高分；
    若已被买爆（涨幅过大）则降分。返回 0-100 分与命中标签。
    """
    haystack = "{} {}".format(row.get("name", ""), row.get("industry", "")).upper()
    hits = [kw for kw in CHOKEPOINT_KEYWORDS if kw.upper() in haystack]
    if not hits:
        return 50.0, []
    sixty_day_pct = coerce_number(row.get("sixty_day_pct"))
    score = 60.0 + min(3, len(hits)) * 8.0  # 上游环节基础加分
    # 尚未被重定价（涨幅温和）→ 加分；已被买爆 → 扣分。
    if -5 <= sixty_day_pct <= 25:
        score += 16
    elif 25 < sixty_day_pct <= 45:
        score += 6
    elif sixty_day_pct > 60:
        score -= 18
    return max(0.0, min(100.0, score)), hits[:3]


def _tech_theme_score(row: pd.Series) -> Tuple[str, float]:
    haystack = "{} {}".format(row.get("name", ""), row.get("industry", "")).upper()
    matches: List[str] = []
    for theme, keywords in TECH_THEMES.items():
        if any(keyword.upper() in haystack for keyword in keywords):
            matches.append(theme)
    if not matches:
        broad_keywords = (
            "科技",
            "电子",
            "通信",
            "光电",
            "光",
            "数据",
            "精密",
            "材料",
            "装备",
            "智能",
            "信息",
            "电源",
            "电路",
            "电气",
        )
        if row.get("market") in ("chinext", "star") or any(
            keyword.upper() in haystack for keyword in broad_keywords
        ):
            return "泛科技/先进制造", 48.0
        return "", 0.0
    score = min(100.0, 58.0 + len(matches) * 12.0)
    return " / ".join(matches[:2]), score
