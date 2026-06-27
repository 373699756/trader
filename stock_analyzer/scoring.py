import os
from datetime import datetime
from typing import Dict, List, Tuple

import pandas as pd

from . import config
from .factor_ic import load_factor_ic
from .normalization import (
    coerce_number,
    finite_series,
    is_supported_code,
    market_type,
    normalize_code,
    percentile_score,
)
from .event_risk import row_event_risk
from .strategy_health import strategy_status


TECH_THEMES = {
    "AI/算力": ("人工智能", "AI", "智能", "算力", "数据", "云", "软件", "信息", "数字", "模型"),
    "半导体": ("半导体", "芯片", "集成", "晶", "微", "芯", "硅", "封装", "存储", "光刻"),
    "机器人/智能制造": ("机器人", "自动化", "机床", "装备", "制造", "工业", "控制", "传感"),
    "低空/商业航天": ("航空", "航天", "导航", "无人机", "低空", "雷达", "飞行"),
    "智能汽车/车联网": ("汽车", "车联", "激光", "毫米波", "电驱", "电控", "线控", "座舱"),
    "新材料/高端电子": ("材料", "复材", "光电", "电子", "陶瓷", "碳", "磁", "膜", "玻璃"),
    "脑机/医疗科技": ("脑机", "神经", "医疗", "器械", "生物", "基因", "康复"),
}

STRATEGY_LABELS = {
    "short_term": "短期推荐",
    "long_term": "长期推荐",
    "tomorrow_picks": "明天预测",
    "tech_potential": "科技潜力",
    "swing_picks": "波段 5-10 日",
    "position_picks": "中长期 1-3 月",
    "chokepoint_picks": "卡脖子",
    "reversal_picks": "反转低波",
    "smallcap_value_picks": "小市值价值",
    "breakout_picks": "量价突破",
}

# Serenity 在量化语境中并不是某个软件库，而是 chokepoint / 瓶颈投资方法论
# （人设 @aleabito，亦见于 UZI-Skill 的 Group I 与 SerenityAlphaTrader）。核心思路：
# 不追被买爆的下游龙头，沿供应链上溯到最难替代、供给最紧、尚未被重定价的“卡脖子”环节。
# 本项目据此为科技潜力策略加入 _chokepoint_score 上游主题倾斜（见 A6）。
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

# 卡脖子/上游环节关键词：供给紧、最难替代、易被市场忽视的供应链上游。
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

# 卡脖子产业链：环节 -> 关键词。用于把命中词归类到环节名，并在前端画产业链全景图。
# 顺序即匹配优先级（靠前的环节先命中）。
CHOKEPOINT_CHAIN = (
    {"segment": "先进光刻/精密光学", "keywords": ("光刻机", "光刻物镜", "投影物镜", "准分子", "精密光学", "光学元件", "光学镜头")},
    {"segment": "半导体设备", "keywords": ("刻蚀", "光刻", "量测", "薄膜沉积", "半导体设备")},
    {"segment": "半导体材料", "keywords": ("光刻胶", "硅片", "衬底", "晶圆", "靶材", "电子特气", "前驱体", "掩膜", "抛光")},
    {"segment": "国产算力芯片/IP", "keywords": ("AI芯片", "GPU", "GPGPU", "CPU", "DPU", "算力芯片", "国产算力", "处理器", "指令集")},
    {"segment": "玻璃基板/TGV", "keywords": ("玻璃基板", "玻璃载板", "玻璃通孔", "TGV", "玻璃基", "封装玻璃")},
    {"segment": "先进封装/HBM", "keywords": ("Chiplet", "HBM", "先进封装", "TSV", "2.5D封装", "3D封装")},
    {"segment": "封装/载板", "keywords": ("封装", "载板", "基板")},
    {"segment": "EDA/IP", "keywords": ("EDA", "IP核")},
    {"segment": "工业软件/CAE", "keywords": ("工业软件", "CAE", "CAD", "PLM", "MES", "仿真软件", "研发设计软件")},
    {"segment": "光器件", "keywords": ("光模块", "光芯片", "光器件")},
    {"segment": "AI算力液冷/电源", "keywords": ("液冷", "温控", "散热", "电源", "UPS", "CPO")},
    {"segment": "高速互连/高频高速PCB", "keywords": ("高速互连", "高频高速", "高速PCB", "铜缆", "连接器", "CCL", "覆铜板")},
    {"segment": "工业母机/高端数控", "keywords": ("工业母机", "数控", "机床", "五轴")},
    {"segment": "高端轴承/丝杠导轨", "keywords": ("轴承", "导轨", "滚珠丝杠", "高端轴承", "直线导轨")},
    {"segment": "机器人核心零部件", "keywords": ("伺服", "减速器", "谐波", "丝杠", "机器人")},
    {"segment": "工业控制/PLC", "keywords": ("DCS", "PLC", "伺服驱动", "运动控制", "工业控制", "工控")},
    {"segment": "SiC/GaN功率半导体", "keywords": ("SiC", "碳化硅", "GaN", "氮化镓", "功率半导体", "IGBT")},
    {"segment": "被动元件/高端电容", "keywords": ("MLCC", "电容", "薄膜电容", "陶瓷电容", "被动元件")},
    {"segment": "高端材料", "keywords": ("陶瓷", "薄膜", "磁材", "永磁", "稀土", "碳", "复材")},
    {"segment": "稀土/关键金属", "keywords": ("稀土", "永磁", "钨", "钼", "钛", "锂", "关键金属")},
    {"segment": "基础软件/信创", "keywords": ("操作系统", "数据库", "中间件", "信创", "基础软件")},
    {"segment": "科学仪器/高端医疗设备", "keywords": ("科学仪器", "测量仪器", "医疗设备", "高端医疗", "基因测序")},
    {"segment": "科研试剂/生物制造", "keywords": ("科研试剂", "生物试剂", "培养基", "工业酶", "合成生物", "原料酶")},
    {"segment": "种业/生物育种", "keywords": ("生物育种", "种业", "转基因", "玉米种子", "水稻种子")},
    {"segment": "高端膜材料/催化剂", "keywords": ("分离膜", "反渗透膜", "催化剂", "吸附树脂", "质子交换膜", "离子交换膜")},
    {"segment": "卫星互联网/低轨星座", "keywords": ("卫星互联网", "低轨卫星", "低轨星座", "中国星网", "国网星座", "千帆", "G60星链", "卫星通信", "卫星载荷", "星载", "相控阵", "卫星终端", "终端天线", "地面站", "射频", "微波", "空间信息")},
    {"segment": "航空航天材料/零部件", "keywords": ("航发", "航空", "航天", "钛合金", "超导", "复材")},
    {"segment": "高端阀门/密封泵", "keywords": ("密封", "阀门", "泵", "高端阀门", "机械密封")},
    {"segment": "精密零部件", "keywords": ("零部件", "元件", "精密", "传感")},
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


TRADING_AGENTS_REFERENCE = {
    "repo": "TauricResearch/TradingAgents",
    "url": "https://github.com/TauricResearch/TradingAgents",
    "adopted": "借鉴分析师团队、牛熊研究辩论、交易员、风控和组合经理的分层决策流",
}

# 可调权重/阈值集中在此，便于回测校准脚本（calibrate.py）离线扫描后写入
# .runtime/weights.json 覆盖，无需改动代码。键路径与下方各策略 final 组合一一对应。
_DEFAULT_WEIGHTS = {
    "short_term": {
        "momentum": 0.55, "liquidity": 0.15, "industry": 0.08,
        "hot": 0.07, "sentiment": 0.15,
        # 反转修正项：A股短线证据显示动量偏弱、反转占优。reversal_tilt>0 时，
        # 对“近期涨太多”按比例减分（0 = 关闭，保持原动量行为）。由 calibrate
        # --compare-momentum 回测决定是否启用及幅度，写入 .runtime/weights.json。
        "reversal_tilt": 0.0,
    },
    "long_term": {
        "trend": 0.42, "liquidity": 0.20, "industry": 0.13,
        "sentiment": 0.13, "momentum": 0.07, "hot": 0.05,
    },
    "tomorrow_picks": {
        "liquidity": 0.30, "momentum": 0.28, "trend": 0.20, "execution": 0.22,
    },
    "swing_picks": {
        "momentum": 0.34, "trend": 0.26, "liquidity": 0.20,
        "execution": 0.12, "not_overextended": 0.08,
    },
    "position_picks": {
        "trend": 0.34, "quality": 0.26, "liquidity": 0.20,
        "theme": 0.12, "execution": 0.08,
    },
    "tech_potential": {
        "theme": 0.24, "chokepoint": 0.10, "liquidity": 0.18,
        "early_trend": 0.20, "not_overextended": 0.14,
        "volume": 0.08, "execution": 0.06,
    },
    "chokepoint_picks": {
        "chokepoint": 0.34, "liquidity": 0.20, "early_trend": 0.18,
        "not_overextended": 0.14, "execution": 0.08, "volume": 0.06,
    },
    "reversal_picks": {
        "oversold_calm": 0.50, "calm_turnover": 0.22, "liquidity": 0.28,
    },
    "smallcap_value_picks": {
        "smallcap": 0.34, "value": 0.22, "liquidity": 0.20,
        "oversold_calm": 0.24,
    },
    "breakout_picks": {
        "momentum": 0.30, "breakout": 0.26, "volume": 0.18,
        "trend": 0.16, "execution": 0.10,
    },
    "regime_profiles": {
        "risk_on": {
            "momentum": 1.12,
            "trend": 1.08,
            "breakout": 1.16,
            "volume": 1.08,
            "lowvol": 0.88,
            "quality": 0.92,
        },
        "risk_off": {
            "momentum": 0.82,
            "trend": 0.94,
            "breakout": 0.78,
            "volume": 0.88,
            "lowvol": 1.18,
            "quality": 1.16,
            "liquidity": 1.08,
        },
        "balanced": {
            "momentum": 0.96,
            "trend": 1.0,
            "breakout": 0.94,
            "volume": 1.0,
            "lowvol": 1.06,
            "quality": 1.04,
        },
    },
}


STRATEGY_COMBINERS = {
    "short_term": {
        "apply_damp": True,
        "terms": (
            {"component": "momentum_score", "weight_key": "momentum", "regime_key": "momentum"},
            {"component": "liquidity_score", "weight_key": "liquidity", "regime_key": "liquidity"},
            {"component": "industry_score", "weight_key": "industry"},
            {"component": "hot_score", "weight_key": "hot"},
            {"component": "sentiment_score", "weight_key": "sentiment"},
        ),
    },
    "long_term": {
        "apply_damp": True,
        "terms": (
            {"component": "trend_score", "weight_key": "trend", "regime_key": "trend"},
            {"component": "liquidity_score", "weight_key": "liquidity", "regime_key": "liquidity"},
            {"component": "industry_score", "weight_key": "industry"},
            {"component": "sentiment_score", "weight_key": "sentiment"},
            {"component": "momentum_score", "weight_key": "momentum", "regime_key": "momentum"},
            {"component": "hot_score", "weight_key": "hot"},
        ),
    },
    "tomorrow_picks": {
        "apply_damp": True,
        "terms": (
            {"component": "liquidity_score", "weight_key": "liquidity", "regime_key": "liquidity"},
            {"component": "momentum_score", "weight_key": "momentum", "regime_key": "momentum"},
            {"component": "trend_score", "weight_key": "trend", "regime_key": "trend"},
            {"component": "execution_score", "weight_key": "execution", "regime_key": "quality"},
        ),
    },
    "swing_picks": {
        "apply_damp": True,
        "terms": (
            {"component": "momentum_score", "weight_key": "momentum", "regime_key": "momentum"},
            {"component": "trend_score", "weight_key": "trend", "regime_key": "trend"},
            {"component": "liquidity_score", "weight_key": "liquidity", "regime_key": "liquidity"},
            {"component": "execution_score", "weight_key": "execution", "regime_key": "quality"},
            {"component": "not_overextended_score", "weight_key": "not_overextended", "regime_key": "quality"},
        ),
    },
    "position_picks": {
        "apply_damp": True,
        "terms": (
            {"component": "trend_score", "weight_key": "trend", "regime_key": "trend"},
            {"component": "quality_proxy_score", "weight_key": "quality", "regime_key": "quality"},
            {"component": "liquidity_score", "weight_key": "liquidity", "regime_key": "liquidity"},
            {"component": "theme_score", "weight_key": "theme"},
            {"component": "execution_score", "weight_key": "execution", "regime_key": "quality"},
        ),
    },
    "tech_potential": {
        "apply_damp": True,
        "terms": (
            {"component": "theme_score", "weight_key": "theme"},
            {"component": "chokepoint_score", "weight_key": "chokepoint"},
            {"component": "liquidity_score", "weight_key": "liquidity", "regime_key": "liquidity"},
            {"component": "early_trend_score", "weight_key": "early_trend", "regime_key": "trend"},
            {"component": "not_overextended_score", "weight_key": "not_overextended", "regime_key": "quality"},
            {"component": "volume_score", "weight_key": "volume", "regime_key": "volume"},
            {"component": "execution_score", "weight_key": "execution", "regime_key": "quality"},
        ),
    },
    "chokepoint_picks": {
        "apply_damp": True,
        "terms": (
            {"component": "chokepoint_score", "weight_key": "chokepoint"},
            {"component": "liquidity_score", "weight_key": "liquidity", "regime_key": "liquidity"},
            {"component": "early_trend_score", "weight_key": "early_trend", "regime_key": "trend"},
            {"component": "not_overextended_score", "weight_key": "not_overextended", "regime_key": "quality"},
            {"component": "execution_score", "weight_key": "execution", "regime_key": "quality"},
            {"component": "volume_score", "weight_key": "volume", "regime_key": "volume"},
        ),
    },
    "reversal_picks": {
        "apply_damp": False,
        "terms": (
            {"component": "oversold_calm_score", "weight_key": "oversold_calm"},
            {"component": "calm_turnover_score", "weight_key": "calm_turnover", "regime_key": "quality"},
            {"component": "liquidity_score", "weight_key": "liquidity", "regime_key": "liquidity"},
        ),
    },
    "smallcap_value_picks": {
        "apply_damp": True,
        "terms": (
            {"component": "smallcap_score", "weight_key": "smallcap"},
            {"component": "value_score", "weight_key": "value"},
            {"component": "liquidity_score", "weight_key": "liquidity", "regime_key": "liquidity"},
            {"component": "oversold_calm_score", "weight_key": "oversold_calm", "regime_key": "lowvol"},
        ),
    },
    "breakout_picks": {
        "apply_damp": True,
        "terms": (
            {"component": "momentum_score", "weight_key": "momentum", "regime_key": "momentum"},
            {"component": "breakout_strength", "weight_key": "breakout", "regime_key": "breakout"},
            {"component": "volume_break_score", "weight_key": "volume", "regime_key": "volume"},
            {"component": "trend_score", "weight_key": "trend", "regime_key": "trend"},
            {"component": "execution_score", "weight_key": "execution", "regime_key": "quality"},
        ),
    },
}


COMPONENT_FACTOR_KEYS = {
    "momentum_score": "momentum_score",
    "trend_score": "trend_score",
    "liquidity_score": "liquidity_score",
    "execution_score": "execution_score",
    "quality_proxy_score": "fundamental_quality_score",
    "value_score": "fundamental_value_score",
    "fundamental_quality_score": "fundamental_quality_score",
    "fundamental_value_score": "fundamental_value_score",
    "earnings_surprise_score": "earnings_surprise_score",
    "rating_revision_score": "rating_revision_score",
}

_FACTOR_IC_CACHE = {"mtime": None, "payload": {}}

# verdict 评级阶梯阈值（参考 UZI 的 80/65/50/35 分档）。
_DEFAULT_THRESHOLDS = {
    "verdict": {"strong_buy": 80.0, "buy": 65.0, "watch": 50.0, "reduce": 35.0},
    # 数据覆盖低于此值的票强制降级 verdict 并打“数据不足”标签。
    "min_data_coverage": 0.5,
    # 过热乘法抑制下限（_not_overextended_score/100 的地板）。
    "overheat_damp_floor": 0.6,
    # 共识极化拉伸系数（一致→拉伸，分歧→压缩）。
    "consensus_stretch_k": 1.3,
}


def _load_weight_overrides() -> Tuple[Dict[str, object], Dict[str, object]]:
    """从 .runtime/weights.json 读取覆盖（存在则深合并到默认值）。任何异常都安全回退到默认。"""
    import copy
    import json
    import os

    weights = copy.deepcopy(_DEFAULT_WEIGHTS)
    thresholds = copy.deepcopy(_DEFAULT_THRESHOLDS)
    path = getattr(config, "WEIGHTS_OVERRIDE_PATH", os.path.join(".runtime", "weights.json"))
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            for group, values in (payload.get("weights") or {}).items():
                if isinstance(values, dict):
                    weights.setdefault(group, {}).update(values)
            for key, value in (payload.get("thresholds") or {}).items():
                default = thresholds.get(key)
                if isinstance(default, dict):
                    # 字典段只接受字典覆盖；标量覆盖会破坏下游下标访问，忽略之。
                    if isinstance(value, dict):
                        default.update(value)
                    continue
                thresholds[key] = value
            # 关键数值阈值兜底，避免非法值（如 k<=0 触发除零）打挂打分流程。
            k = thresholds.get("consensus_stretch_k")
            if not isinstance(k, (int, float)) or k <= 0:
                thresholds["consensus_stretch_k"] = _DEFAULT_THRESHOLDS["consensus_stretch_k"]
            cov = thresholds.get("min_data_coverage")
            if not isinstance(cov, (int, float)) or not (0.0 <= cov <= 1.0):
                thresholds["min_data_coverage"] = _DEFAULT_THRESHOLDS["min_data_coverage"]
            floor = thresholds.get("overheat_damp_floor")
            if not isinstance(floor, (int, float)) or not (0.0 <= floor <= 1.0):
                thresholds["overheat_damp_floor"] = _DEFAULT_THRESHOLDS["overheat_damp_floor"]
    except Exception:
        return copy.deepcopy(_DEFAULT_WEIGHTS), copy.deepcopy(_DEFAULT_THRESHOLDS)
    return weights, thresholds


WEIGHTS, THRESHOLDS = _load_weight_overrides()

PROFILE_COMPONENTS = (
    ("momentum_score", "动量"),
    ("trend_score", "趋势"),
    ("liquidity_score", "流动性"),
    ("execution_score", "买入安全"),
    ("theme_score", "主题"),
    ("sentiment_score", "舆情"),
    ("industry_score", "行业"),
    ("not_overextended_score", "不过热"),
    ("quality_proxy_score", "质量代理"),
    ("fundamental_quality_score", "基本面质量"),
    ("fundamental_value_score", "估值"),
    ("earnings_surprise_score", "业绩超预期"),
    ("early_trend_score", "启动趋势"),
)

ALPHALITE_SIGNAL_COLUMNS = (
    "ret_3d",
    "ret_5d",
    "ret_10d",
    "ret_20d",
    "ma5_gap",
    "ma20_gap",
    "vol_amount_5d",
    "breakout_20d",
    "volatility_20d",
)


def prepare_candidates(quotes: pd.DataFrame) -> pd.DataFrame:
    if quotes.empty:
        return quotes.copy()
    df = quotes.copy()
    if "code" not in df.columns:
        raise ValueError("行情数据缺少代码字段")
    if "name" not in df.columns:
        df["name"] = ""

    df["code"] = df["code"].map(normalize_code)
    df["name"] = df["name"].astype(str)
    df["market"] = df["code"].map(market_type)
    for column in (
        "price",
        "pct_chg",
        "change",
        "volume",
        "turnover",
        "amplitude",
        "high",
        "low",
        "open",
        "prev_close",
        "volume_ratio",
        "turnover_rate",
        "speed",
        "five_min_pct",
        "sixty_day_pct",
        "ytd_pct",
        "float_market_cap",
        "market_cap",
        "pe_dynamic",
        "pb",
    ):
        if column not in df.columns:
            df[column] = 0.0
        df[column] = df[column].map(coerce_number)

    if "industry" not in df.columns:
        for candidate in ("所属行业", "行业", "板块"):
            if candidate in df.columns:
                df["industry"] = df[candidate].astype(str)
                break
        else:
            df["industry"] = ""

    mask = df["code"].map(is_supported_code)
    mask &= ~df["name"].str.contains("ST|退", case=False, regex=True, na=False)
    mask &= df["price"] > 0
    mask &= df["turnover"] >= config.MIN_TURNOVER
    mask &= df["pct_chg"] > -8
    mask &= df["pct_chg"] <= config.MAX_RECOMMENDED_GAIN
    mask &= df.apply(_is_buyable_gain, axis=1)
    mask &= ~((df["high"] > 0) & (df["high"] == df["low"]) & (df["pct_chg"] > 8))
    return df.loc[mask].reset_index(drop=True)


def _is_buyable_gain(row: pd.Series) -> bool:
    pct = coerce_number(row.get("pct_chg"))
    market = row.get("market")
    if market in ("chinext", "star"):
        return pct <= config.MAX_BUYABLE_GAIN_GROWTH
    return pct <= config.MAX_BUYABLE_GAIN_MAIN


def build_market_regime(df: pd.DataFrame, breadth_source: pd.DataFrame = None) -> Dict[str, object]:
    breadth_df = _market_regime_breadth_frame(breadth_source) if breadth_source is not None else df
    if breadth_df.empty:
        breadth_df = df
    if df.empty and breadth_df.empty:
        return {
            "level": "unknown",
            "label": "未知",
            "score": 50.0,
            "breadth_pct": 0.0,
            "strong_pct": 0.0,
            "weak_pct": 0.0,
            "median_pct_chg": 0.0,
            "avg_amplitude": 0.0,
            "avg_turnover": 0.0,
            "leaders": [],
            "advice": "暂无足够样本判断当前盘面环境。",
        }

    pct_values = finite_series(breadth_df, "pct_chg")
    amplitude_values = finite_series(df, "amplitude")
    turnover_values = finite_series(df, "turnover")
    breadth_pct = round(float((pct_values > 0).mean() * 100), 2) if len(pct_values) else 0.0
    strong_pct = round(float((pct_values >= 3).mean() * 100), 2) if len(pct_values) else 0.0
    weak_pct = round(float((pct_values <= -3).mean() * 100), 2) if len(pct_values) else 0.0
    median_pct_chg = round(coerce_number(pct_values.median()), 2) if len(pct_values) else 0.0
    avg_amplitude = round(coerce_number(amplitude_values.mean()), 2) if len(amplitude_values) else 0.0
    avg_turnover = round(coerce_number(turnover_values.mean()), 2) if len(turnover_values) else 0.0

    score = 50.0
    score += median_pct_chg * 7.5
    score += (breadth_pct - 50.0) * 0.55
    score += (strong_pct - weak_pct) * 0.35
    score -= max(0.0, avg_amplitude - 7.0) * 2.4
    score = round(max(0.0, min(100.0, score)), 2)

    if score >= 68:
        level = "risk_on"
        label = "偏进攻"
        advice = "盘面承接较强，优先看强势延续与多策略共识标的。"
    elif score <= 42:
        level = "risk_off"
        label = "偏防守"
        advice = "盘面分歧偏大，优先看稳健趋势与低追高风险标的。"
    else:
        level = "balanced"
        label = "均衡震荡"
        advice = "盘面没有明显单边优势，优先看流动性和验证样本更好的策略。"

    leaders: List[Dict[str, object]] = []
    for market in ("main", "chinext", "star"):
        subset = breadth_df[breadth_df["market"] == market]
        if subset.empty:
            continue
        market_pct = finite_series(subset, "pct_chg")
        leaders.append(
            {
                "market": market,
                "market_label": config.MARKET_LABELS.get(market, market),
                "breadth_pct": round(float((market_pct > 0).mean() * 100), 2) if len(market_pct) else 0.0,
                "median_pct_chg": round(coerce_number(market_pct.median()), 2) if len(market_pct) else 0.0,
                "count": int(len(subset)),
            }
        )
    leaders.sort(key=lambda item: (item["median_pct_chg"], item["breadth_pct"]), reverse=True)

    return {
        "level": level,
        "label": label,
        "score": score,
        "breadth_pct": breadth_pct,
        "strong_pct": strong_pct,
        "weak_pct": weak_pct,
        "median_pct_chg": median_pct_chg,
        "avg_amplitude": avg_amplitude,
        "avg_turnover": avg_turnover,
        "leaders": leaders[:3],
        "advice": advice,
    }


def _market_regime_breadth_frame(quotes: pd.DataFrame) -> pd.DataFrame:
    if quotes is None or quotes.empty:
        return pd.DataFrame()
    df = quotes.copy()
    if "code" not in df.columns:
        return pd.DataFrame()
    if "name" not in df.columns:
        df["name"] = ""
    df["code"] = df["code"].map(normalize_code)
    if "market" not in df.columns:
        df["market"] = df["code"].map(market_type)
    if "price" not in df.columns:
        df["price"] = 0.0
    if "pct_chg" not in df.columns:
        df["pct_chg"] = 0.0
    df["price"] = df["price"].map(coerce_number)
    df["pct_chg"] = df["pct_chg"].map(coerce_number)
    mask = df["code"].map(is_supported_code)
    mask &= ~df["name"].astype(str).str.contains("ST|退", case=False, regex=True, na=False)
    mask &= df["price"] > 0
    return df.loc[mask].reset_index(drop=True)


def _stddev(values: List[float]) -> float:
    nums = [coerce_number(v) for v in values if pd.notna(coerce_number(v))]
    if len(nums) < 2:
        return 0.0
    mean = sum(nums) / len(nums)
    variance = sum((v - mean) ** 2 for v in nums) / len(nums)
    return variance ** 0.5


def _consensus_stretch(score: float, agreement: float) -> float:
    """A3：以 50 为中心做极化拉伸。

    agreement≈1（策略高度一致）→ 把分数朝两端推；agreement≈0（互相打架）→ 朝 50 压缩。
    k 取 THRESHOLDS.consensus_stretch_k（默认 1.3）。
    """
    k = coerce_number(THRESHOLDS.get("consensus_stretch_k"), 1.3)
    if k <= 0:  # 防御非法覆盖导致的除零
        k = 1.3
    # gain 在 [1/k, k] 之间随一致性线性变化。
    gain = (1.0 / k) + (k - 1.0 / k) * max(0.0, min(1.0, agreement))
    return 50.0 + (score - 50.0) * gain


def _strategy_reliability(strategy_metrics: Dict[str, Dict[str, object]]) -> Dict[str, float]:
    """B2：把各策略真实样本净表现折成可信度乘子。

    优先使用真实前瞻样本的主周期净胜率/净收益；真实样本不足时才轻度参考
    回放样本，避免历史回放把共识权重虚高。
    """
    reliability: Dict[str, float] = {}
    if not strategy_metrics:
        return reliability
    for name, metrics in strategy_metrics.items():
        if not isinstance(metrics, dict):
            continue
        real_samples = int(metrics.get("real_sample_count") or 0)
        replay_samples = int(metrics.get("replay_sample_count") or 0)
        total_samples = int(metrics.get("sample_count") or 0)
        health = strategy_status(metrics)
        health_state = health.get("state")
        health_weight = 1.0
        if health_state == "retired":
            reliability[name] = 0.0
            continue
        if health_state == "probation":
            health_weight = max(0.0, min(1.0, coerce_number(getattr(config, "STRATEGY_DECAY_SOFT_WEIGHT", 0.5), 0.5)))
        if real_samples >= 10:
            win_rate = metrics.get("real_win_rate_primary_net")
            avg_return = metrics.get("real_avg_primary_return_net")
            confidence = min(1.0, real_samples / 30.0)
        elif total_samples >= 20 and replay_samples > 0:
            win_rate = metrics.get("win_rate_primary_net")
            avg_return = metrics.get("avg_primary_return_net")
            confidence = min(0.45, total_samples / 120.0)
        else:
            win_rate = metrics.get("win_rate_next_close")
            avg_return = metrics.get("avg_next_close_return")
            confidence = min(0.35, total_samples / 60.0) if total_samples else 0.0
        if win_rate is None or confidence <= 0:
            continue
        win_component = (coerce_number(win_rate) - 50.0) / 10.0 * 0.04
        return_component = coerce_number(avg_return) * 0.015
        multiplier = 1.0 + (win_component + return_component) * confidence
        reliability[name] = max(0.0, min(1.2, multiplier * health_weight))
    return reliability


def build_strategy_consensus(
    strategy_rows: Dict[str, List[Dict[str, object]]],
    minimum_appearances: int = 2,
    top_n: int = 10,
    strategy_metrics: Dict[str, Dict[str, object]] = None,
) -> List[Dict[str, object]]:
    available_strategies = [name for name, rows in strategy_rows.items() if rows]
    strategy_count = len(available_strategies)
    if strategy_count <= 0:
        return []

    # B2：把各策略的历史命中率折成可信度乘子（0.8~1.2），命中率高的策略票更被采信。
    reliability = _strategy_reliability(strategy_metrics)

    merged: Dict[str, Dict[str, object]] = {}
    for strategy_name, rows in strategy_rows.items():
        if not rows:
            continue
        strategy_label = STRATEGY_LABELS.get(strategy_name, strategy_name)
        weight = reliability.get(strategy_name, 1.0)
        if weight <= 0:
            continue
        for row in rows[:top_n]:
            code = str(row.get("code") or "").strip()
            if not code:
                continue
            item = merged.setdefault(
                code,
                {
                    "code": code,
                    "name": str(row.get("name", "")),
                    "market_label": str(row.get("market_label", row.get("market", ""))),
                    "theme": str(row.get("theme", "")),
                    "appearances": 0,
                    "rank_total": 0.0,
                    "score_total": 0.0,
                    "quality_total": 0.0,
                    "confidence_total": 0.0,
                    "risk_total": 0.0,
                    "agent_total": 0.0,
                    "best_rank": 999.0,
                    "strategies": [],
                    "strategy_names": [],
                    "evidence": [],
                    "actions": [],
                    "agent_actions": [],
                    "member_scores": [],
                    "reliability_total": 0.0,
                },
            )
            profile = row.get("serenity_profile") or {}
            committee = row.get("agent_committee") or {}
            item["appearances"] += 1
            item["rank_total"] += coerce_number(row.get("rank"), 99.0)
            item["score_total"] += coerce_number(row.get("score"))
            item["quality_total"] += coerce_number(profile.get("quality_score"), row.get("score"))
            item["confidence_total"] += coerce_number(profile.get("confidence_score"), 50.0)
            item["risk_total"] += coerce_number(profile.get("risk_score"), 50.0)
            item["agent_total"] += coerce_number(committee.get("final_score"), profile.get("quality_score", row.get("score")))
            item["best_rank"] = min(coerce_number(item.get("best_rank"), 999.0), coerce_number(row.get("rank"), 99.0))
            item["member_scores"].append(coerce_number(row.get("score")))
            item["reliability_total"] += weight
            if strategy_label not in item["strategies"]:
                item["strategies"].append(strategy_label)
            if strategy_name not in item["strategy_names"]:
                item["strategy_names"].append(strategy_name)
            if not item.get("theme") and row.get("theme"):
                item["theme"] = str(row.get("theme"))
            for evidence in profile.get("evidence", [])[:2]:
                text = str(evidence.get("label", ""))
                if text and text not in item["evidence"]:
                    item["evidence"].append(text)
            action = str(profile.get("action_label", ""))
            if action and action not in item["actions"]:
                item["actions"].append(action)
            agent_action = str(committee.get("final_action_label", ""))
            if agent_action and agent_action not in item["agent_actions"]:
                item["agent_actions"].append(agent_action)

    rows: List[Dict[str, object]] = []
    for item in merged.values():
        appearances = int(item["appearances"])
        if appearances < minimum_appearances:
            continue
        avg_rank = item["rank_total"] / max(appearances, 1)
        avg_score = item["score_total"] / max(appearances, 1)
        avg_quality = item["quality_total"] / max(appearances, 1)
        avg_confidence = item["confidence_total"] / max(appearances, 1)
        avg_risk = item["risk_total"] / max(appearances, 1)
        avg_agent_score = item["agent_total"] / max(appearances, 1)
        ratio = round(appearances / strategy_count * 100, 2)
        if appearances >= 4:
            level, label = "high", "强共识"
        elif appearances >= 3:
            level, label = "medium", "中共识"
        else:
            level, label = "low", "弱共识"

        # A3：跨策略分数离散度 → 一致性。一致(低离散)拉伸、分歧(高离散)压缩。
        member_scores = item.get("member_scores") or [avg_score]
        dispersion = _stddev(member_scores)
        agreement = round(max(0.0, min(1.0, 1.0 - dispersion / 25.0)), 3)
        # B2：可信度均值（>1 表示该票多来自高命中率策略）。
        reliability_avg = item["reliability_total"] / max(appearances, 1)

        base_consensus = (
            avg_score * 0.32
            + avg_quality * 0.25
            + avg_confidence * 0.15
            + avg_agent_score * 0.12
            + (110.0 - avg_rank * 8.0) * 0.16
            - max(0.0, avg_risk - 55.0) * 0.35
        )
        consensus_score = round(
            max(0.0, min(100.0, _consensus_stretch(base_consensus, agreement) * reliability_avg)),
            2,
        )
        action_label = _consensus_action(label, avg_quality, avg_confidence, avg_risk, avg_agent_score)
        rows.append(
            {
                "code": item["code"],
                "name": item["name"],
                "market_label": item["market_label"],
                "theme": item["theme"],
                "appearances": appearances,
                "strategy_count": strategy_count,
                "consensus_ratio": ratio,
                "best_rank": int(item["best_rank"]),
                "avg_rank": round(avg_rank, 2),
                "avg_score": round(avg_score, 2),
                "avg_quality": round(avg_quality, 2),
                "avg_confidence": round(avg_confidence, 2),
                "avg_risk": round(avg_risk, 2),
                "avg_agent_score": round(avg_agent_score, 2),
                "dispersion": round(dispersion, 2),
                "agreement": agreement,
                "reliability": round(reliability_avg, 3),
                "consensus_score": consensus_score,
                "level": level,
                "label": label,
                "action_label": action_label,
                "evidence": item["evidence"][:4],
                "actions": item["actions"][:3],
                "agent_actions": item["agent_actions"][:3],
                "strategies": item["strategies"],
                "strategy_names": item["strategy_names"],
            }
        )

    rows.sort(
        key=lambda item: (
            item["appearances"],
            item["consensus_score"],
            -item["avg_risk"],
            -item["avg_rank"],
        ),
        reverse=True,
    )
    return rows[:top_n]


def _consensus_action(label: str, quality: float, confidence: float, risk: float, agent_score: float = 50.0) -> str:
    if risk >= 72:
        return "只观察"
    if quality >= 72 and confidence >= 62 and risk <= 48 and agent_score >= 66:
        return "{}优先".format(label)
    if quality >= 62 and risk <= 58 and agent_score >= 56:
        return "小仓跟踪"
    return "等待确认"


def score_candidates(
    df: pd.DataFrame,
    hot_ranks: Dict[str, int],
    industry_strength: Dict[str, float],
    sentiment_lookup: Dict[str, Dict[str, object]],
    top_n: int,
    market_filter: str = "all",
) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    if market_filter in ("main", "chinext", "star"):
        df = df[df["market"] == market_filter].copy()
    if df.empty:
        return [], {"generated_at": datetime.now().isoformat(timespec="seconds")}

    pct_values = finite_series(df, "pct_chg").tolist()
    speed_values = _combined_speed(df).tolist()
    volume_ratio_values = finite_series(df, "volume_ratio").tolist()
    turnover_rate_values = finite_series(df, "turnover_rate").tolist()
    turnover_values = finite_series(df, "turnover").tolist()
    industry_values = list(industry_strength.values())

    rows: List[Dict[str, object]] = []
    for _, row in df.iterrows():
        code = row["code"]
        industry = str(row.get("industry", "") or "")
        pct_chg = coerce_number(row.get("pct_chg"))
        speed = _row_speed(row)
        volume_ratio = coerce_number(row.get("volume_ratio"))
        turnover_rate = coerce_number(row.get("turnover_rate"))
        turnover = coerce_number(row.get("turnover"))
        industry_pct = industry_strength.get(industry, 0.0)
        hot_rank = hot_ranks.get(code)
        sentiment = sentiment_lookup.get(code, {"score": 50.0, "summary": "未拉取到个股舆情"})

        momentum_score = (
            percentile_score(pct_chg, pct_values) * 0.38
            + percentile_score(speed, speed_values) * 0.32
            + percentile_score(volume_ratio, volume_ratio_values) * 0.30
        )
        liquidity_score = (
            percentile_score(turnover_rate, turnover_rate_values) * 0.45
            + percentile_score(turnover, turnover_values) * 0.55
        )
        industry_score = percentile_score(industry_pct, industry_values) if industry_values else 50.0
        hot_score = _hot_rank_score(hot_rank)
        sentiment_score = coerce_number(sentiment.get("score"), 50.0)

        final_score = (
            momentum_score * 0.55
            + liquidity_score * 0.15
            + industry_score * 0.08
            + hot_score * 0.07
            + sentiment_score * 0.15
        )
        if sentiment.get("risk_words"):
            final_score -= 8
        if _near_limit_up_risk(row):
            final_score -= 5

        rows.append(
            {
                "code": code,
                "name": str(row.get("name", "")),
                "market": row.get("market", "main"),
                "market_label": config.MARKET_LABELS.get(row.get("market", "main"), "主板"),
                "industry": industry,
                "price": round(coerce_number(row.get("price")), 3),
                "pct_chg": round(pct_chg, 2),
                "speed": round(coerce_number(row.get("speed")), 2),
                "five_min_pct": round(coerce_number(row.get("five_min_pct")), 2),
                "volume_ratio": round(volume_ratio, 2),
                "turnover_rate": round(turnover_rate, 2),
                "turnover": round(turnover, 2),
                "industry_pct": round(industry_pct, 2),
                "hot_rank": hot_rank,
                "momentum_score": round(momentum_score, 2),
                "liquidity_score": round(liquidity_score, 2),
                "industry_score": round(industry_score, 2),
                "sentiment_score": round(sentiment_score, 2),
                "score": round(max(0.0, min(100.0, final_score)), 2),
                "sentiment_summary": sentiment.get("summary", "暂无明显舆情信号"),
                "risk_words": sentiment.get("risk_words", []),
                "reasons": _build_reasons(row, industry_pct, hot_rank, sentiment),
            }
        )

    rows.sort(key=lambda item: item["score"], reverse=True)
    for rank, row in enumerate(rows[:top_n], start=1):
        row["rank"] = rank

    meta = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "candidate_count": len(df),
        "top_n": top_n,
        "market_filter": market_filter,
    }
    return rows[:top_n], meta


def score_dual_horizon_candidates(
    df: pd.DataFrame,
    hot_ranks: Dict[str, int],
    industry_strength: Dict[str, float],
    sentiment_lookup: Dict[str, Dict[str, object]],
    top_n: int = 10,
    market_filter: str = "all",
    market_regime: Dict[str, object] = None,
) -> Tuple[Dict[str, List[Dict[str, object]]], Dict[str, object]]:
    if market_filter in ("main", "chinext", "star"):
        df = df[df["market"] == market_filter].copy()
    if df.empty:
        return {"short_term": [], "long_term": []}, {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "candidate_count": 0,
            "top_n": top_n,
            "market_filter": market_filter,
        }

    context = _score_context(df, industry_strength)
    short_rows: List[Dict[str, object]] = []
    long_rows: List[Dict[str, object]] = []
    for _, row in df.iterrows():
        short_rows.append(
            _score_row(
                row,
                hot_ranks=hot_ranks,
                industry_strength=industry_strength,
                sentiment_lookup=sentiment_lookup,
                context=context,
                horizon="short",
                market_regime=market_regime,
            )
        )
        long_rows.append(
            _score_row(
                row,
                hot_ranks=hot_ranks,
                industry_strength=industry_strength,
                sentiment_lookup=sentiment_lookup,
                context=context,
                horizon="long",
                market_regime=market_regime,
            )
        )

    short_rows.sort(key=lambda item: item["score"], reverse=True)
    long_rows.sort(key=lambda item: item["score"], reverse=True)
    for rank, row in enumerate(short_rows[:top_n], start=1):
        row["rank"] = rank
    for rank, row in enumerate(long_rows[:top_n], start=1):
        row["rank"] = rank

    meta = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "candidate_count": len(df),
        "top_n": top_n,
        "market_filter": market_filter,
        "strategy": {
            "short_term": "盘中强势：涨跌幅、涨速、量比、换手、热度、舆情",
            "long_term": "趋势稳健：60日/YTD趋势、流动性、板块、舆情、风险惩罚",
        },
    }
    return {"short_term": short_rows[:top_n], "long_term": long_rows[:top_n]}, meta


def score_tomorrow_candidates(
    df: pd.DataFrame,
    top_n: int = 50,
    market_filter: str = "all",
    market_regime: Dict[str, object] = None,
) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    if market_filter in ("main", "chinext", "star"):
        df = df[df["market"] == market_filter].copy()
    if df.empty:
        return [], {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "candidate_count": 0,
            "top_n": top_n,
            "market_filter": market_filter,
            "analysis_window": "14:30",
            "strategy_version": "tomorrow_picks_v2",
            "strategy_label": "明天预测",
            "policy": _tomorrow_policy(),
        }

    context = _score_context(df, {})
    rows: List[Dict[str, object]] = []
    for _, row in df.iterrows():
        pct_chg = coerce_number(row.get("pct_chg"))
        volume_ratio = coerce_number(row.get("volume_ratio"))
        turnover_rate = coerce_number(row.get("turnover_rate"))
        turnover = coerce_number(row.get("turnover"))
        speed = _row_speed(row)
        amplitude = coerce_number(row.get("amplitude"))
        sixty_day_pct = coerce_number(row.get("sixty_day_pct"))
        ytd_pct = coerce_number(row.get("ytd_pct"))

        liquidity_score = (
            percentile_score(turnover, context["turnover_values"]) * 0.58
            + percentile_score(turnover_rate, context["turnover_rate_values"]) * 0.42
        )
        momentum_score = (
            percentile_score(pct_chg, context["pct_values"]) * 0.34
            + percentile_score(speed, context["speed_values"]) * 0.24
            + percentile_score(volume_ratio, context["volume_ratio_values"]) * 0.24
            + _optional_factor_score(sixty_day_pct, context["sixty_day_values"]) * 0.18
        )
        trend_score = (
            percentile_score(sixty_day_pct, context["sixty_day_values"]) * 0.55
            + percentile_score(ytd_pct, context["ytd_values"]) * 0.25
            + _optional_factor_score(
                amplitude,
                context["amplitude_values"],
                higher_is_better=False,
            ) * 0.20
        )
        execution_score = _execution_score(row)
        risk_penalty_parts = _tomorrow_risk_penalty_parts(row)
        risk_penalty = _sum_penalty(risk_penalty_parts)
        regime_bonus = _market_regime_adjustment(row, market_regime, "tomorrow")
        regime_profile = _regime_weight_profile(market_regime, ["liquidity", "momentum", "trend", "quality"])
        combined = _combine_details(
            {
                "liquidity_score": liquidity_score,
                "momentum_score": momentum_score,
                "trend_score": trend_score,
                "execution_score": execution_score,
                "risk_penalty": risk_penalty,
                "regime_bonus": regime_bonus,
            },
            "tomorrow_picks",
            market_regime=market_regime,
            row=row,
        )
        final_score = combined["score"]
        item = {
                "code": row["code"],
                "name": str(row.get("name", "")),
                "market": row.get("market", "main"),
                "market_label": config.MARKET_LABELS.get(row.get("market", "main"), "主板"),
                "industry": str(row.get("industry", "") or ""),
                "price": round(coerce_number(row.get("price")), 3),
                "pct_chg": round(pct_chg, 2),
                "speed": round(coerce_number(row.get("speed")), 2),
                "five_min_pct": round(coerce_number(row.get("five_min_pct")), 2),
                "volume_ratio": round(volume_ratio, 2),
                "turnover_rate": round(turnover_rate, 2),
                "turnover": round(turnover, 2),
                "sixty_day_pct": round(sixty_day_pct, 2),
                "ytd_pct": round(ytd_pct, 2),
                "amplitude": round(amplitude, 2),
                "liquidity_score": round(liquidity_score, 2),
                "momentum_score": round(momentum_score, 2),
                "trend_score": round(trend_score, 2),
                "execution_score": round(execution_score, 2),
                "risk_penalty": round(risk_penalty, 2),
                "risk_penalty_parts": risk_penalty_parts,
                "regime_bonus": round(regime_bonus, 2),
                "regime_weight_profile": regime_profile,
                "base_score": round(combined["base_score"], 2),
                "raw_score": round(combined["raw_score"], 2),
                "overheat_damp": round(combined["overheat_damp"], 4),
                "score": round(max(0.0, min(100.0, final_score)), 2),
                "reasons": _build_tomorrow_reasons(
                    row,
                    liquidity_score,
                    momentum_score,
                    trend_score,
                    execution_score,
                    risk_penalty,
                ),
        }
        rows.append(
            _with_regime_reason(
                _attach_signal_explanation(
                    item,
                    row,
                    "tomorrow_picks",
                    "明天预测",
                    "次日冲高",
                ),
                market_regime,
                regime_bonus,
            )
        )

    rows.sort(key=lambda item: item["score"], reverse=True)
    for rank, row in enumerate(rows[:top_n], start=1):
        row["rank"] = rank
    return rows[:top_n], {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "candidate_count": len(df),
        "top_n": top_n,
        "market_filter": market_filter,
        "analysis_window": "14:30",
        "strategy_version": "tomorrow_picks_v2",
        "strategy_label": "明天预测",
        "strategy": "14:30 明天预测：剔除涨停/近涨停，综合成交额、换手、量比、当日强度、中期趋势和买入安全风险",
        "policy": _tomorrow_policy(),
    }


def score_tech_potential_candidates(
    df: pd.DataFrame,
    top_n: int = 50,
    market_filter: str = "all",
    market_regime: Dict[str, object] = None,
) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    if market_filter in ("main", "chinext", "star"):
        df = df[df["market"] == market_filter].copy()
    df = df[
        (finite_series(df, "sixty_day_pct") <= 90)
        & (finite_series(df, "ytd_pct") <= 150)
        & (finite_series(df, "sixty_day_pct") >= -25)
    ].copy()
    if df.empty:
        return [], {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "candidate_count": 0,
            "top_n": top_n,
            "market_filter": market_filter,
        }

    context = _score_context(df, {})
    rows: List[Dict[str, object]] = []
    for _, row in df.iterrows():
        theme, theme_score = _tech_theme_score(row)
        if theme_score <= 0:
            continue
        pct_chg = coerce_number(row.get("pct_chg"))
        turnover = coerce_number(row.get("turnover"))
        turnover_rate = coerce_number(row.get("turnover_rate"))
        volume_ratio = coerce_number(row.get("volume_ratio"))
        sixty_day_pct = coerce_number(row.get("sixty_day_pct"))
        ytd_pct = coerce_number(row.get("ytd_pct"))
        amplitude = coerce_number(row.get("amplitude"))

        liquidity_score = (
            percentile_score(turnover, context["turnover_values"]) * 0.62
            + percentile_score(turnover_rate, context["turnover_rate_values"]) * 0.38
        )
        early_trend_score = _early_trend_score(row)
        volume_score = _balanced_volume_score(volume_ratio)
        valuation_proxy_score = _not_overextended_score(row)
        execution_score = _execution_score(row)
        chokepoint_score, chokepoint_hits = _chokepoint_score(row)
        risk_penalty_parts = _tech_potential_risk_penalty_parts(row)
        risk_penalty = _sum_penalty(risk_penalty_parts)
        regime_bonus = _market_regime_adjustment(row, market_regime, "tech")
        regime_profile = _regime_weight_profile(market_regime, ["liquidity", "trend", "volume", "quality"])
        combined = _combine_details(
            {
                "theme_score": theme_score,
                "chokepoint_score": chokepoint_score,
                "liquidity_score": liquidity_score,
                "early_trend_score": early_trend_score,
                "not_overextended_score": valuation_proxy_score,
                "volume_score": volume_score,
                "execution_score": execution_score,
                "risk_penalty": risk_penalty,
                "regime_bonus": regime_bonus,
            },
            "tech_potential",
            market_regime=market_regime,
            row=row,
        )
        final_score = combined["score"]
        item = {
                "code": row["code"],
                "name": str(row.get("name", "")),
                "market": row.get("market", "main"),
                "market_label": config.MARKET_LABELS.get(row.get("market", "main"), "主板"),
                "industry": str(row.get("industry", "") or ""),
                "theme": theme,
                "market_cap": round(coerce_number(row.get("market_cap")), 2),
                "float_market_cap": round(coerce_number(row.get("float_market_cap")), 2),
                "price": round(coerce_number(row.get("price")), 3),
                "pct_chg": round(pct_chg, 2),
                "volume_ratio": round(volume_ratio, 2),
                "turnover_rate": round(turnover_rate, 2),
                "turnover": round(turnover, 2),
                "sixty_day_pct": round(sixty_day_pct, 2),
                "ytd_pct": round(ytd_pct, 2),
                "amplitude": round(amplitude, 2),
                "theme_score": round(theme_score, 2),
                "chokepoint_score": round(chokepoint_score, 2),
                "chokepoint_hits": chokepoint_hits,
                "liquidity_score": round(liquidity_score, 2),
                "early_trend_score": round(early_trend_score, 2),
                "not_overextended_score": round(valuation_proxy_score, 2),
                "volume_score": round(volume_score, 2),
                "execution_score": round(execution_score, 2),
                "risk_penalty": round(risk_penalty, 2),
                "risk_penalty_parts": risk_penalty_parts,
                "regime_bonus": round(regime_bonus, 2),
                "regime_weight_profile": regime_profile,
                "base_score": round(combined["base_score"], 2),
                "raw_score": round(combined["raw_score"], 2),
                "overheat_damp": round(combined["overheat_damp"], 4),
                "score": round(max(0.0, min(100.0, final_score)), 2),
                "reasons": _build_tech_potential_reasons(
                    row,
                    theme,
                    early_trend_score,
                    valuation_proxy_score,
                    liquidity_score,
                    risk_penalty,
                ),
        }
        rows.append(
            _with_regime_reason(
                _attach_signal_explanation(
                    item,
                    row,
                    "tech_potential",
                    "科技潜力",
                    "科技主题潜力",
                ),
                market_regime,
                regime_bonus,
            )
        )

    rows.sort(key=lambda item: item["score"], reverse=True)
    for rank, row in enumerate(rows[:top_n], start=1):
        row["rank"] = rank
    return rows[:top_n], {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "candidate_count": len(df),
        "matched_count": len(rows),
        "top_n": top_n,
        "market_filter": market_filter,
        "strategy": "科技潜力：匹配前沿科技方向，排除涨幅透支，偏好刚启动、流动性足、买入安全较好的潜力股",
    }


def score_chokepoint_candidates(
    df: pd.DataFrame,
    top_n: int = 30,
    market_filter: str = "all",
    market_regime: Dict[str, object] = None,
) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    """卡脖子策略：以 _chokepoint_score 为主导，挑供给紧、难替代、尚未被重定价的上游环节。

    与科技潜力的区别：chokepoint 是主导因子（0.34）而非副权重，且只保留命中上游关键词的票，
    并把命中词归类到产业链环节（chain_segment），meta 里附 chain 用于前端产业链全景图。
    """
    if market_filter in ("main", "chinext", "star"):
        df = df[df["market"] == market_filter].copy()
    df = df[
        (finite_series(df, "sixty_day_pct") <= 90)
        & (finite_series(df, "ytd_pct") <= 150)
        & (finite_series(df, "sixty_day_pct") >= -25)
    ].copy()
    if df.empty:
        return [], {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "candidate_count": 0,
            "matched_count": 0,
            "top_n": top_n,
            "market_filter": market_filter,
            "chain": [],
        }

    context = _score_context(df, {})
    rows: List[Dict[str, object]] = []
    for _, row in df.iterrows():
        chokepoint_score, chokepoint_hits = _chokepoint_score(row)
        if not chokepoint_hits:
            continue  # 只保留真正落在卡脖子环节的票
        pct_chg = coerce_number(row.get("pct_chg"))
        turnover = coerce_number(row.get("turnover"))
        turnover_rate = coerce_number(row.get("turnover_rate"))
        volume_ratio = coerce_number(row.get("volume_ratio"))
        sixty_day_pct = coerce_number(row.get("sixty_day_pct"))
        ytd_pct = coerce_number(row.get("ytd_pct"))
        amplitude = coerce_number(row.get("amplitude"))
        chain_segment = _chain_segment(chokepoint_hits)

        liquidity_score = (
            percentile_score(turnover, context["turnover_values"]) * 0.62
            + percentile_score(turnover_rate, context["turnover_rate_values"]) * 0.38
        )
        early_trend_score = _early_trend_score(row)
        volume_score = _balanced_volume_score(volume_ratio)
        not_overextended_score = _not_overextended_score(row)
        execution_score = _execution_score(row)
        risk_penalty_parts = _tech_potential_risk_penalty_parts(row)
        risk_penalty = _sum_penalty(risk_penalty_parts)
        regime_bonus = _market_regime_adjustment(row, market_regime, "tech")
        regime_profile = _regime_weight_profile(market_regime, ["liquidity", "trend", "volume", "quality"])
        combined = _combine_details(
            {
                "chokepoint_score": chokepoint_score,
                "liquidity_score": liquidity_score,
                "early_trend_score": early_trend_score,
                "not_overextended_score": not_overextended_score,
                "execution_score": execution_score,
                "volume_score": volume_score,
                "risk_penalty": risk_penalty,
                "regime_bonus": regime_bonus,
            },
            "chokepoint_picks",
            market_regime=market_regime,
            row=row,
        )
        final_score = combined["score"]
        item = {
                "code": row["code"],
                "name": str(row.get("name", "")),
                "market": row.get("market", "main"),
                "market_label": config.MARKET_LABELS.get(row.get("market", "main"), "主板"),
                "industry": str(row.get("industry", "") or ""),
                "theme": chain_segment,
                "chain_segment": chain_segment,
                "market_cap": round(coerce_number(row.get("market_cap")), 2),
                "float_market_cap": round(coerce_number(row.get("float_market_cap")), 2),
                "price": round(coerce_number(row.get("price")), 3),
                "pct_chg": round(pct_chg, 2),
                "volume_ratio": round(volume_ratio, 2),
                "turnover_rate": round(turnover_rate, 2),
                "turnover": round(turnover, 2),
                "sixty_day_pct": round(sixty_day_pct, 2),
                "ytd_pct": round(ytd_pct, 2),
                "amplitude": round(amplitude, 2),
                "chokepoint_score": round(chokepoint_score, 2),
                "chokepoint_hits": chokepoint_hits,
                "liquidity_score": round(liquidity_score, 2),
                "early_trend_score": round(early_trend_score, 2),
                "not_overextended_score": round(not_overextended_score, 2),
                "volume_score": round(volume_score, 2),
                "execution_score": round(execution_score, 2),
                "risk_penalty": round(risk_penalty, 2),
                "risk_penalty_parts": risk_penalty_parts,
                "regime_bonus": round(regime_bonus, 2),
                "regime_weight_profile": regime_profile,
                "base_score": round(combined["base_score"], 2),
                "raw_score": round(combined["raw_score"], 2),
                "overheat_damp": round(combined["overheat_damp"], 4),
                "score": round(max(0.0, min(100.0, final_score)), 2),
                "reasons": [
                    "卡脖子环节：{}".format(chain_segment),
                    "命中：{}".format("、".join(chokepoint_hits)),
                ],
        }
        rows.append(
            _with_regime_reason(
                _attach_signal_explanation(
                    item,
                    row,
                    "chokepoint_picks",
                    "卡脖子",
                    "卡脖子上游",
                ),
                market_regime,
                regime_bonus,
            )
        )

    rows.sort(key=lambda item: item["score"], reverse=True)
    for rank, row in enumerate(rows[:top_n], start=1):
        row["rank"] = rank

    # 产业链全景：按环节分组，每组列出当日命中该环节的 top 票（即使没进总 top_n）。
    chain_map: Dict[str, List[Dict[str, object]]] = {}
    for row in rows:
        chain_map.setdefault(row["chain_segment"], []).append(row)
    chain = []
    for node in CHOKEPOINT_CHAIN:
        members = chain_map.get(node["segment"], [])
        chain.append({
            "segment": node["segment"],
            "count": len(members),
            "picks": [
                {
                    "code": m["code"],
                    "name": m["name"],
                    "score": m["score"],
                    "verdict": m.get("verdict"),
                    "pct_chg": m["pct_chg"],
                }
                for m in members[:6]
            ],
        })
    other = chain_map.get("其他上游", [])
    if other:
        chain.append({
            "segment": "其他上游",
            "count": len(other),
            "picks": [
                {"code": m["code"], "name": m["name"], "score": m["score"], "verdict": m.get("verdict"), "pct_chg": m["pct_chg"]}
                for m in other[:6]
            ],
        })

    return rows[:top_n], {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "candidate_count": len(df),
        "matched_count": len(rows),
        "top_n": top_n,
        "market_filter": market_filter,
        "strategy_version": "chokepoint_v1",
        "strategy": "卡脖子：上溯供应链，挖掘供给最紧、最难替代、尚未被重定价的上游环节",
        "chain": chain,
    }


def score_swing_candidates(
    df: pd.DataFrame,
    top_n: int = 30,
    market_filter: str = "all",
    market_regime: Dict[str, object] = None,
) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    if market_filter in ("main", "chinext", "star"):
        df = df[df["market"] == market_filter].copy()
    df = df[
        (finite_series(df, "pct_chg") <= 8)
        & (finite_series(df, "sixty_day_pct") <= 85)
        & (finite_series(df, "ytd_pct") <= 130)
        & (finite_series(df, "sixty_day_pct") >= -18)
    ].copy()
    if df.empty:
        return [], _horizon_meta(top_n, market_filter, 0, "swing_5_10d_v1", "波段 5-10 日")

    context = _score_context(df, {})
    rows: List[Dict[str, object]] = []
    for _, row in df.iterrows():
        ret_5d = coerce_number(row.get("ret_5d"))
        ret_10d = coerce_number(row.get("ret_10d"))
        ret_20d = coerce_number(row.get("ret_20d"))
        ma5_gap = coerce_number(row.get("ma5_gap"))
        ma20_gap = coerce_number(row.get("ma20_gap"))
        vol_amount_5d = coerce_number(row.get("vol_amount_5d"))
        breakout_20d = coerce_number(row.get("breakout_20d"))
        volatility_20d = coerce_number(row.get("volatility_20d"))
        pct_chg = coerce_number(row.get("pct_chg"))
        turnover = coerce_number(row.get("turnover"))
        turnover_rate = coerce_number(row.get("turnover_rate"))
        volume_ratio = coerce_number(row.get("volume_ratio"))
        sixty_day_pct = coerce_number(row.get("sixty_day_pct"))
        ytd_pct = coerce_number(row.get("ytd_pct"))

        momentum_score = (
            _optional_factor_score(ret_5d, context["ret_5d_values"], fallback=pct_chg, fallback_values=context["pct_values"]) * 0.24
            + _optional_factor_score(ret_10d, context["ret_10d_values"], fallback=sixty_day_pct, fallback_values=context["sixty_day_values"]) * 0.22
            + _optional_factor_score(ma5_gap, context["ma5_gap_values"], fallback=pct_chg, fallback_values=context["pct_values"]) * 0.16
            + _optional_factor_score(vol_amount_5d, context["vol_amount_5d_values"], fallback=volume_ratio, fallback_values=context["volume_ratio_values"]) * 0.18
            + percentile_score(volume_ratio, context["volume_ratio_values"]) * 0.12
            + _optional_factor_score(breakout_20d, context["breakout_20d_values"]) * 0.08
        )
        trend_score = (
            _optional_factor_score(ret_20d, context["ret_20d_values"], fallback=sixty_day_pct, fallback_values=context["sixty_day_values"]) * 0.30
            + percentile_score(sixty_day_pct, context["sixty_day_values"]) * 0.26
            + _optional_factor_score(ma20_gap, context["ma20_gap_values"], fallback=sixty_day_pct, fallback_values=context["sixty_day_values"]) * 0.22
            + percentile_score(ytd_pct, context["ytd_values"]) * 0.10
            + _optional_factor_score(volatility_20d, context["volatility_20d_values"], higher_is_better=False, fallback=coerce_number(row.get("amplitude")), fallback_values=context["amplitude_values"]) * 0.12
        )
        liquidity_score = (
            percentile_score(turnover, context["turnover_values"]) * 0.62
            + percentile_score(turnover_rate, context["turnover_rate_values"]) * 0.38
        )
        execution_score = _execution_score(row)
        risk_penalty_parts = _swing_risk_penalty_parts(row)
        risk_penalty = _sum_penalty(risk_penalty_parts)
        regime_bonus = _market_regime_adjustment(row, market_regime, "swing")
        not_overextended_score = _not_overextended_score(row)
        regime_profile = _regime_weight_profile(market_regime, ["momentum", "trend", "liquidity", "quality"])
        combined = _combine_details(
            {
                "momentum_score": momentum_score,
                "trend_score": trend_score,
                "liquidity_score": liquidity_score,
                "execution_score": execution_score,
                "not_overextended_score": not_overextended_score,
                "risk_penalty": risk_penalty,
                "regime_bonus": regime_bonus,
            },
            "swing_picks",
            market_regime=market_regime,
            row=row,
        )
        final_score = combined["score"]
        item = _horizon_row(row, {
            "ret_5d": ret_5d,
            "ret_10d": ret_10d,
            "ret_20d": ret_20d,
            "ma5_gap": ma5_gap,
            "ma20_gap": ma20_gap,
            "vol_amount_5d": vol_amount_5d,
            "breakout_20d": bool(breakout_20d),
            "volatility_20d": volatility_20d,
            "momentum_score": momentum_score,
            "trend_score": trend_score,
            "liquidity_score": liquidity_score,
            "execution_score": execution_score,
            "not_overextended_score": not_overextended_score,
            "risk_penalty": risk_penalty,
            "risk_penalty_parts": risk_penalty_parts,
            "regime_bonus": regime_bonus,
            "regime_weight_profile": regime_profile,
            "base_score": combined["base_score"],
            "raw_score": combined["raw_score"],
            "overheat_damp": combined["overheat_damp"],
            "score": final_score,
            "horizon": "swing",
            "reasons": _build_swing_reasons(row, momentum_score, trend_score, liquidity_score, risk_penalty),
        })
        rows.append(
            _with_regime_reason(
                _attach_signal_explanation(item, row, "swing_picks", "波段 5-10 日", "波段延续"),
                market_regime,
                regime_bonus,
            )
        )

    rows.sort(key=lambda item: item["score"], reverse=True)
    for rank, row in enumerate(rows[:top_n], start=1):
        row["rank"] = rank
    meta = _horizon_meta(len(rows[:top_n]), market_filter, len(df), "swing_5_10d_v1", "波段 5-10 日")
    meta["strategy"] = "波段 5-10 日：偏好5/10/20日趋势延续、温和放量、站上短均线、流动性足且涨幅未透支"
    return rows[:top_n], meta


def score_position_candidates(
    df: pd.DataFrame,
    top_n: int = 30,
    market_filter: str = "all",
    market_regime: Dict[str, object] = None,
) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    if market_filter in ("main", "chinext", "star"):
        df = df[df["market"] == market_filter].copy()
    df = df[
        (finite_series(df, "pct_chg") <= 6)
        & (finite_series(df, "sixty_day_pct") <= 75)
        & (finite_series(df, "ytd_pct") <= 120)
        & (finite_series(df, "sixty_day_pct") >= -25)
    ].copy()
    if df.empty:
        return [], _horizon_meta(top_n, market_filter, 0, "position_1_3m_v1", "中长期 1-3 月")

    context = _score_context(df, {})
    rows: List[Dict[str, object]] = []
    for _, row in df.iterrows():
        ret_20d = coerce_number(row.get("ret_20d"))
        ret_10d = coerce_number(row.get("ret_10d"))
        ma20_gap = coerce_number(row.get("ma20_gap"))
        vol_amount_5d = coerce_number(row.get("vol_amount_5d"))
        volatility_20d = coerce_number(row.get("volatility_20d"))
        turnover = coerce_number(row.get("turnover"))
        turnover_rate = coerce_number(row.get("turnover_rate"))
        sixty_day_pct = coerce_number(row.get("sixty_day_pct"))
        ytd_pct = coerce_number(row.get("ytd_pct"))
        amplitude = coerce_number(row.get("amplitude"))
        theme, theme_score = _tech_theme_score(row)
        if not theme:
            theme = "行业/趋势"
            theme_score = 50.0

        trend_score = (
            percentile_score(sixty_day_pct, context["sixty_day_values"]) * 0.24
            + percentile_score(ytd_pct, context["ytd_values"]) * 0.18
            + _optional_factor_score(ret_20d, context["ret_20d_values"], fallback=sixty_day_pct, fallback_values=context["sixty_day_values"]) * 0.24
            + _optional_factor_score(ret_10d, context["ret_10d_values"], fallback=sixty_day_pct, fallback_values=context["sixty_day_values"]) * 0.12
            + _optional_factor_score(ma20_gap, context["ma20_gap_values"], fallback=sixty_day_pct, fallback_values=context["sixty_day_values"]) * 0.14
            + _optional_factor_score(volatility_20d, context["volatility_20d_values"], higher_is_better=False, fallback=amplitude, fallback_values=context["amplitude_values"]) * 0.08
        )
        quality_proxy_score = (
            _not_overextended_score(row) * 0.50
            + _optional_factor_score(volatility_20d, context["volatility_20d_values"], higher_is_better=False, fallback=amplitude, fallback_values=context["amplitude_values"]) * 0.25
            + _balanced_volume_score(coerce_number(row.get("volume_ratio"))) * 0.15
            + _optional_factor_score(vol_amount_5d, context["vol_amount_5d_values"], fallback=coerce_number(row.get("volume_ratio")), fallback_values=context["volume_ratio_values"]) * 0.10
        )
        fundamental_blend = _weighted_score(
            (
                (row.get("fundamental_quality_score"), 0.45),
                (row.get("fundamental_value_score"), 0.30),
                (row.get("earnings_surprise_score"), 0.15),
                (row.get("rating_revision_score"), 0.10),
            ),
            fallback=0.0,
        )
        if fundamental_blend > 0:
            quality_proxy_score = quality_proxy_score * 0.78 + fundamental_blend * 0.22
        liquidity_score = (
            percentile_score(turnover, context["turnover_values"]) * 0.68
            + percentile_score(turnover_rate, context["turnover_rate_values"]) * 0.32
        )
        risk_penalty_parts = _position_risk_penalty_parts(row)
        risk_penalty = _sum_penalty(risk_penalty_parts)
        regime_bonus = _market_regime_adjustment(row, market_regime, "position")
        execution_score = _execution_score(row)
        regime_profile = _regime_weight_profile(market_regime, ["trend", "quality", "liquidity"])
        combined = _combine_details(
            {
                "trend_score": trend_score,
                "quality_proxy_score": quality_proxy_score,
                "liquidity_score": liquidity_score,
                "theme_score": theme_score,
                "execution_score": execution_score,
                "risk_penalty": risk_penalty,
                "regime_bonus": regime_bonus,
            },
            "position_picks",
            market_regime=market_regime,
            row=row,
        )
        final_score = combined["score"]
        item = _horizon_row(row, {
            "theme": theme,
            "theme_score": theme_score,
            "ret_10d": ret_10d,
            "ret_20d": ret_20d,
            "ma20_gap": ma20_gap,
            "vol_amount_5d": vol_amount_5d,
            "volatility_20d": volatility_20d,
            "trend_score": trend_score,
            "quality_proxy_score": quality_proxy_score,
            "fundamental_quality_score": coerce_number(row.get("fundamental_quality_score")),
            "fundamental_value_score": coerce_number(row.get("fundamental_value_score")),
            "earnings_surprise_score": coerce_number(row.get("earnings_surprise_score")),
            "rating_revision_score": coerce_number(row.get("rating_revision_score")),
            "liquidity_score": liquidity_score,
            "execution_score": execution_score,
            "risk_penalty": risk_penalty,
            "risk_penalty_parts": risk_penalty_parts,
            "regime_bonus": regime_bonus,
            "regime_weight_profile": regime_profile,
            "base_score": combined["base_score"],
            "raw_score": combined["raw_score"],
            "overheat_damp": combined["overheat_damp"],
            "score": final_score,
            "horizon": "position",
            "reasons": _build_position_reasons(row, theme, trend_score, quality_proxy_score, liquidity_score, risk_penalty),
        })
        rows.append(
            _with_regime_reason(
                _attach_signal_explanation(item, row, "position_picks", "中长期 1-3 月", "中期趋势"),
                market_regime,
                regime_bonus,
            )
        )

    rows.sort(key=lambda item: item["score"], reverse=True)
    for rank, row in enumerate(rows[:top_n], start=1):
        row["rank"] = rank
    meta = _horizon_meta(len(rows[:top_n]), market_filter, len(df), "position_1_3m_v1", "中长期 1-3 月")
    meta["strategy"] = "中长期 1-3 月：中期趋势 + 可选基本面质量/价值增强，偏好趋势向上、波动可控、涨幅未透支、流动性充足"
    meta["limitation"] = "未接入财务时仅用量价代理；基本面因子默认关闭，开启后仍需真实样本和 IC 验证，不等同于财务投资建议。"
    return rows[:top_n], meta


def score_reversal_candidates(
    df: pd.DataFrame,
    top_n: int = 30,
    market_filter: str = "all",
    market_regime: Dict[str, object] = None,
) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    """反转·低波·高换手回避因子股。

    依据 A 股横截面证据（短线反转 + 低波动 + 高换手未来弱）。主导因子：
    近期跌得多(反转)、波动低、换手不过热。不加过热 damp（本就偏低位）。
    """
    if market_filter in ("main", "chinext", "star"):
        df = df[df["market"] == market_filter].copy()
    if df.empty:
        return [], _basic_meta(top_n, market_filter, "reversal_v1")

    context = _score_context(df, {})
    rows: List[Dict[str, object]] = []
    for _, row in df.iterrows():
        ret_20d = coerce_number(row.get("ret_20d"))
        volatility_20d = coerce_number(row.get("volatility_20d"))
        turnover_rate = coerce_number(row.get("turnover_rate"))
        turnover = coerce_number(row.get("turnover"))
        sixty_day_pct = coerce_number(row.get("sixty_day_pct"))

        # 反转：近期跌得多→分高（history 关时回退 sixty_day_pct）。
        reversal_score = _optional_factor_score(
            ret_20d, context["ret_20d_values"], higher_is_better=False,
            fallback=sixty_day_pct, fallback_values=context["sixty_day_values"],
        )
        lowvol_score = _optional_factor_score(
            volatility_20d, context["volatility_20d_values"], higher_is_better=False,
            fallback=coerce_number(row.get("amplitude")), fallback_values=context["amplitude_values"],
        )
        calm_turnover_score = percentile_score(turnover_rate, context["turnover_rate_values"], higher_is_better=False)
        liquidity_score = percentile_score(turnover, context["turnover_values"])
        not_overextended = _not_overextended_score(row)
        oversold_calm_score = _composite_score([reversal_score, lowvol_score, not_overextended])
        risk_penalty_parts = _reversal_risk_penalty_parts(row)
        risk_penalty = _sum_penalty(risk_penalty_parts)
        regime_bonus = _market_regime_adjustment(row, market_regime, "long")
        regime_profile = _regime_weight_profile(market_regime, ["lowvol", "quality", "liquidity"])
        combined = _combine_details(
            {
                "oversold_calm_score": oversold_calm_score,
                "calm_turnover_score": calm_turnover_score,
                "liquidity_score": liquidity_score,
                "risk_penalty": risk_penalty,
                "regime_bonus": regime_bonus,
            },
            "reversal_picks",
            market_regime=market_regime,
            row=row,
        )
        final_score = combined["score"]
        item = {
            "code": row["code"],
            "name": str(row.get("name", "")),
            "market": row.get("market", "main"),
            "market_label": config.MARKET_LABELS.get(row.get("market", "main"), "主板"),
            "industry": str(row.get("industry", "") or ""),
            "market_cap": round(coerce_number(row.get("market_cap")), 2),
            "float_market_cap": round(coerce_number(row.get("float_market_cap")), 2),
            "price": round(coerce_number(row.get("price")), 3),
            "pct_chg": round(coerce_number(row.get("pct_chg")), 2),
            "volume_ratio": round(coerce_number(row.get("volume_ratio")), 2),
            "turnover": round(turnover, 2),
            "turnover_rate": round(turnover_rate, 2),
            "sixty_day_pct": round(sixty_day_pct, 2),
            "ytd_pct": round(coerce_number(row.get("ytd_pct")), 2),
            "ret_20d": round(ret_20d, 2),
            "volatility_20d": round(volatility_20d, 2),
            "reversal_score": round(reversal_score, 2),
            "lowvol_score": round(lowvol_score, 2),
            "oversold_calm_score": round(oversold_calm_score, 2),
            "calm_turnover_score": round(calm_turnover_score, 2),
            "liquidity_score": round(liquidity_score, 2),
            "not_overextended_score": round(not_overextended, 2),
            "risk_penalty": round(risk_penalty, 2),
            "risk_penalty_parts": risk_penalty_parts,
            "regime_bonus": round(regime_bonus, 2),
            "regime_weight_profile": regime_profile,
            "base_score": round(combined["base_score"], 2),
            "raw_score": round(combined["raw_score"], 2),
            "overheat_damp": round(combined["overheat_damp"], 4),
            "score": round(max(0.0, min(100.0, final_score)), 2),
            "reasons": [
                "超跌冷静复合分 {:.0f}（反转/低波/不过热）".format(oversold_calm_score),
                "换手不过热分 {:.0f}、流动性分 {:.0f}".format(calm_turnover_score, liquidity_score),
            ],
        }
        rows.append(
            _with_regime_reason(
                _attach_signal_explanation(item, row, "reversal_picks", "反转低波", "超跌反转"),
                market_regime, regime_bonus,
            )
        )

    rows.sort(key=lambda item: item["score"], reverse=True)
    for rank, row in enumerate(rows[:top_n], start=1):
        row["rank"] = rank
    meta = _basic_meta(top_n, market_filter, "reversal_v1")
    meta["candidate_count"] = len(df)
    meta["matched_count"] = len(rows)
    meta["factor_correlation"] = _reversal_factor_correlation(context)
    meta["strategy"] = "反转低波：A股短线反转+低波动+高换手回避，挖掘超跌且不躁动的标的"
    return rows[:top_n], meta


def score_smallcap_value_candidates(
    df: pd.DataFrame,
    top_n: int = 30,
    market_filter: str = "all",
    market_regime: Dict[str, object] = None,
) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    """小市值·价值股。A股历史最强因子之一，但带 2024 微盘崩盘尾风险 → 多重护栏。"""
    if market_filter in ("main", "chinext", "star"):
        df = df[df["market"] == market_filter].copy()
    # 护栏：需有有效流通市值与正 PE/PB；过滤亏损与超小市值（壳/退市风险）。
    if "float_market_cap" not in df.columns:
        return [], _basic_meta(top_n, market_filter, "smallcap_value_v1", note="当前行情源未提供市值/估值字段，建议使用东方财富源")
    df = df[
        (finite_series(df, "float_market_cap") >= config.SMALLCAP_MIN_FLOAT_CAP)
        & (finite_series(df, "pe_dynamic") > 0)
        & (finite_series(df, "pb") > 0)
    ].copy()
    if df.empty:
        return [], _basic_meta(top_n, market_filter, "smallcap_value_v1", note="无满足市值/估值护栏的标的")

    context = _score_context(df, {})
    rows: List[Dict[str, object]] = []
    for _, row in df.iterrows():
        float_cap = coerce_number(row.get("float_market_cap"))
        pe = coerce_number(row.get("pe_dynamic"))
        pb = coerce_number(row.get("pb"))
        turnover = coerce_number(row.get("turnover"))
        volatility_20d = coerce_number(row.get("volatility_20d"))

        smallcap_score = percentile_score(float_cap, context["float_market_cap_values"], higher_is_better=False)
        value_score = (
            percentile_score(pe, context["pe_dynamic_values"], higher_is_better=False) * 0.5
            + percentile_score(pb, context["pb_values"], higher_is_better=False) * 0.5
        )
        liquidity_score = percentile_score(turnover, context["turnover_values"])
        lowvol_score = _optional_factor_score(
            volatility_20d, context["volatility_20d_values"], higher_is_better=False,
            fallback=coerce_number(row.get("amplitude")), fallback_values=context["amplitude_values"],
        )
        not_overextended = _not_overextended_score(row)
        oversold_calm_score = _composite_score([lowvol_score, not_overextended])
        risk_penalty_parts = _position_risk_penalty_parts(row)
        risk_penalty = _sum_penalty(risk_penalty_parts)
        # 市场偏防守时小市值整体降权（尾风险最大的时候）。
        regime_bonus = _market_regime_adjustment(row, market_regime, "position")
        regime_profile = _regime_weight_profile(market_regime, ["liquidity", "lowvol", "quality"])
        combined = _combine_details(
            {
                "smallcap_score": smallcap_score,
                "value_score": value_score,
                "liquidity_score": liquidity_score,
                "oversold_calm_score": oversold_calm_score,
                "risk_penalty": risk_penalty,
                "regime_bonus": regime_bonus,
            },
            "smallcap_value_picks",
            market_regime=market_regime,
            row=row,
        )
        final_score = combined["score"]
        item = {
            "code": row["code"],
            "name": str(row.get("name", "")),
            "market": row.get("market", "main"),
            "market_label": config.MARKET_LABELS.get(row.get("market", "main"), "主板"),
            "industry": str(row.get("industry", "") or ""),
            "market_cap": round(coerce_number(row.get("market_cap")), 2),
            "price": round(coerce_number(row.get("price")), 3),
            "pct_chg": round(coerce_number(row.get("pct_chg")), 2),
            "volume_ratio": round(coerce_number(row.get("volume_ratio")), 2),
            "turnover": round(turnover, 2),
            "turnover_rate": round(coerce_number(row.get("turnover_rate")), 2),
            "float_market_cap": round(float_cap, 2),
            "pe_dynamic": round(pe, 2),
            "pb": round(pb, 2),
            "sixty_day_pct": round(coerce_number(row.get("sixty_day_pct")), 2),
            "ytd_pct": round(coerce_number(row.get("ytd_pct")), 2),
            "smallcap_score": round(smallcap_score, 2),
            "value_score": round(value_score, 2),
            "liquidity_score": round(liquidity_score, 2),
            "lowvol_score": round(lowvol_score, 2),
            "not_overextended_score": round(not_overextended, 2),
            "oversold_calm_score": round(oversold_calm_score, 2),
            "risk_penalty": round(risk_penalty, 2),
            "risk_penalty_parts": risk_penalty_parts,
            "regime_bonus": round(regime_bonus, 2),
            "regime_weight_profile": regime_profile,
            "base_score": round(combined["base_score"], 2),
            "raw_score": round(combined["raw_score"], 2),
            "overheat_damp": round(combined["overheat_damp"], 4),
            "score": round(max(0.0, min(100.0, final_score)), 2),
            "reasons": [
                "流通市值 {:.1f} 亿、PE {:.1f}、PB {:.2f}".format(float_cap / 1e8, pe, pb),
                "小市值分 {:.0f}、价值分 {:.0f}、稳定分 {:.0f}".format(smallcap_score, value_score, oversold_calm_score),
            ],
        }
        rows.append(
            _with_regime_reason(
                _attach_signal_explanation(item, row, "smallcap_value_picks", "小市值价值", "小市值低估"),
                market_regime, regime_bonus,
            )
        )

    rows.sort(key=lambda item: item["score"], reverse=True)
    for rank, row in enumerate(rows[:top_n], start=1):
        row["rank"] = rank
    meta = _basic_meta(top_n, market_filter, "smallcap_value_v1")
    meta["candidate_count"] = len(df)
    meta["matched_count"] = len(rows)
    meta["strategy"] = "小市值价值：低流通市值+低PE/PB，含市值下限/亏损过滤/流动性/防守降权护栏"
    meta["risk_note"] = "小市值因子有 2024 年初微盘股流动性崩盘的尾风险；偏防守市况已自动降权，仍建议分散并设止损。"
    return rows[:top_n], meta


def score_breakout_candidates(
    df: pd.DataFrame,
    top_n: int = 30,
    market_filter: str = "all",
    market_regime: Dict[str, object] = None,
) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    """量价突破·均线多头。经典技术派趋势确认：多头排列或20日新高 + 量能突破。"""
    if market_filter in ("main", "chinext", "star"):
        df = df[df["market"] == market_filter].copy()
    if df.empty:
        return [], _basic_meta(top_n, market_filter, "breakout_v1")

    context = _score_context(df, {})
    history_factor_available = any(
        finite_series(df, column).abs().sum() > 0
        for column in ("ret_20d", "ma5_gap", "ma20_gap", "vol_ma5_ratio", "volatility_20d")
    )
    has_history_breakout_signal = bool(
        finite_series(df, "breakout_20d").abs().sum() > 0
        or finite_series(df, "ma_bull_aligned").abs().sum() > 0
    )
    rows: List[Dict[str, object]] = []
    for _, row in df.iterrows():
        breakout_20d = coerce_number(row.get("breakout_20d"))
        ma_bull = coerce_number(row.get("ma_bull_aligned"))
        row_history_ready = _row_history_factor_ready(row)
        vol_ma5_ratio = coerce_number(row.get("vol_ma5_ratio"))
        pct_chg = coerce_number(row.get("pct_chg"))
        speed = _row_speed(row)
        volume_ratio = coerce_number(row.get("volume_ratio"))
        turnover = coerce_number(row.get("turnover"))
        sixty_day_pct = coerce_number(row.get("sixty_day_pct"))
        fallback_breakout = False
        if breakout_20d < 0.5 and ma_bull < 0.5:
            fallback_breakout = _realtime_breakout_proxy(row)
            if not fallback_breakout:
                continue

        momentum_score = (
            percentile_score(pct_chg, context["pct_values"]) * 0.5
            + percentile_score(speed, context["speed_values"]) * 0.5
        )
        trend_score = (
            percentile_score(sixty_day_pct, context["sixty_day_values"]) * 0.6
            + _optional_factor_score(coerce_number(row.get("ma20_gap")), context["ma20_gap_values"]) * 0.4
        )
        # 突破强度：多头排列 + 创新高各自加分。
        breakout_strength = 50.0 + ma_bull * 25.0 + breakout_20d * 25.0
        if fallback_breakout:
            breakout_strength = max(
                breakout_strength,
                54.0
                + min(18.0, max(0.0, pct_chg) * 2.2)
                + min(14.0, max(0.0, volume_ratio - 1.0) * 8.0)
                + min(10.0, max(0.0, sixty_day_pct) * 0.22),
            )
        # 量能突破：vol_ma5_ratio>=1.5 加分（history 关时回退量比）。
        if vol_ma5_ratio > 0:
            volume_break = min(100.0, 40.0 + max(0.0, vol_ma5_ratio - 1.0) * 40.0)
        else:
            volume_break = _balanced_volume_score(volume_ratio)
        execution_score = _execution_score(row)
        risk_penalty_parts = _tomorrow_risk_penalty_parts(row)
        risk_penalty = _sum_penalty(risk_penalty_parts)
        regime_bonus = _market_regime_adjustment(row, market_regime, "swing")
        regime_profile = _regime_weight_profile(market_regime, ["momentum", "breakout", "volume", "trend", "quality"])
        combined = _combine_details(
            {
                "momentum_score": momentum_score,
                "breakout_strength": breakout_strength,
                "volume_break_score": volume_break,
                "trend_score": trend_score,
                "execution_score": execution_score,
                "risk_penalty": risk_penalty,
                "regime_bonus": regime_bonus,
            },
            "breakout_picks",
            market_regime=market_regime,
            row=row,
        )
        final_score = combined["score"]
        item = {
            "code": row["code"],
            "name": str(row.get("name", "")),
            "market": row.get("market", "main"),
            "market_label": config.MARKET_LABELS.get(row.get("market", "main"), "主板"),
            "industry": str(row.get("industry", "") or ""),
            "market_cap": round(coerce_number(row.get("market_cap")), 2),
            "float_market_cap": round(coerce_number(row.get("float_market_cap")), 2),
            "price": round(coerce_number(row.get("price")), 3),
            "pct_chg": round(pct_chg, 2),
            "volume_ratio": round(volume_ratio, 2),
            "turnover": round(turnover, 2),
            "turnover_rate": round(coerce_number(row.get("turnover_rate")), 2),
            "sixty_day_pct": round(sixty_day_pct, 2),
            "ytd_pct": round(coerce_number(row.get("ytd_pct")), 2),
            "breakout_20d": bool(breakout_20d),
            "ma_bull_aligned": bool(ma_bull),
            "breakout_fallback": bool(fallback_breakout),
            "history_factor_ready": bool(row_history_ready),
            "vol_ma5_ratio": round(vol_ma5_ratio, 2),
            "momentum_score": round(momentum_score, 2),
            "breakout_strength": round(breakout_strength, 2),
            "volume_break_score": round(volume_break, 2),
            "trend_score": round(trend_score, 2),
            "execution_score": round(execution_score, 2),
            "risk_penalty": round(risk_penalty, 2),
            "risk_penalty_parts": risk_penalty_parts,
            "regime_bonus": round(regime_bonus, 2),
            "regime_weight_profile": regime_profile,
            "base_score": round(combined["base_score"], 2),
            "raw_score": round(combined["raw_score"], 2),
            "overheat_damp": round(combined["overheat_damp"], 4),
            "score": round(max(0.0, min(100.0, final_score)), 2),
            "reasons": [
                "{}{}".format("均线多头排列 " if ma_bull >= 0.5 else "", "创20日新高" if breakout_20d >= 0.5 else "").strip()
                or ("实时强势兜底" if fallback_breakout else "趋势确认"),
                "量能突破 {:.1f}×5日均量".format(vol_ma5_ratio) if vol_ma5_ratio > 0 else "量比 {:.1f}".format(volume_ratio),
            ],
        }
        rows.append(
            _with_regime_reason(
                _attach_signal_explanation(item, row, "breakout_picks", "量价突破", "突破确认"),
                market_regime, regime_bonus,
            )
        )

    rows.sort(key=lambda item: item["score"], reverse=True)
    for rank, row in enumerate(rows[:top_n], start=1):
        row["rank"] = rank
    meta = _basic_meta(top_n, market_filter, "breakout_v1")
    meta["candidate_count"] = len(df)
    meta["matched_count"] = len(rows)
    meta["history_factor_available"] = history_factor_available
    meta["history_signal_available"] = has_history_breakout_signal
    meta["fallback_count"] = sum(1 for row in rows if row.get("breakout_fallback"))
    if not history_factor_available:
        meta["note"] = "当前未覆盖均线/20日新高历史因子，已使用实时涨幅、涨速、量比和60日趋势做兜底筛选。"
    elif meta["fallback_count"]:
        meta["note"] = "部分股票历史突破因子未完整覆盖，已允许实时放量强势票作为兜底候选。"
    meta["strategy"] = "量价突破：均线多头排列或20日新高 + 量能突破，趋势确认型选股"
    return rows[:top_n], meta


def _row_history_factor_ready(row: pd.Series) -> bool:
    if coerce_number(row.get("alphalite_factor_ready")) > 0:
        return True
    return any(
        abs(coerce_number(row.get(column))) > 1e-12
        for column in ("ret_20d", "ma5_gap", "ma20_gap", "vol_ma5_ratio", "volatility_20d", "breakout_20d", "ma_bull_aligned")
    )


def _realtime_breakout_proxy(row: pd.Series) -> bool:
    """历史均线因子缺失时的保守兜底：只承认实时强势+放量+流动性充足。"""
    pct = coerce_number(row.get("pct_chg"))
    speed = _row_speed(row)
    volume_ratio = coerce_number(row.get("volume_ratio"))
    turnover = coerce_number(row.get("turnover"))
    sixty_day_pct = coerce_number(row.get("sixty_day_pct"))
    amplitude = coerce_number(row.get("amplitude"))
    if turnover < config.MIN_TURNOVER * 2:
        return False
    if pct <= 0 or pct > config.MAX_BUYABLE_GAIN_GROWTH:
        return False
    if volume_ratio < 1.35:
        return False
    if speed < 0.2 and sixty_day_pct < 8:
        return False
    if amplitude > 12:
        return False
    return True


def _basic_meta(top_n: int, market_filter: str, version: str, note: str = "") -> Dict[str, object]:
    meta = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "candidate_count": 0,
        "matched_count": 0,
        "top_n": top_n,
        "market_filter": market_filter,
        "strategy_version": version,
    }
    if note:
        meta["note"] = note
    return meta


def _reversal_risk_penalty(row: pd.Series) -> float:
    return _sum_penalty(_reversal_risk_penalty_parts(row))


def _reversal_risk_penalty_parts(row: pd.Series) -> Dict[str, float]:
    """反转策略风险：连续大跌+放量下杀（基本面崩坏迹象）扣分，避免接飞刀。"""
    ret_20d = coerce_number(row.get("ret_20d"))
    ret_5d = coerce_number(row.get("ret_5d"))
    volume_ratio = coerce_number(row.get("volume_ratio"))
    pct = coerce_number(row.get("pct_chg"))
    parts = {}
    if ret_20d < -40:
        parts["crash_drawdown"] = 12  # 20日腰斩级别，可能有实质利空
    elif ret_20d < -28:
        parts["crash_drawdown"] = 6
    if ret_5d < -18 and volume_ratio > 2.5:
        parts["volume_selloff"] = 8  # 近期放量急杀
    if pct < -6:
        parts["intraday_drop"] = 4  # 当日仍在大跌
    return parts


def _score_context(df: pd.DataFrame, industry_strength: Dict[str, float]) -> Dict[str, List[float]]:
    return {
        "pct_values": finite_series(df, "pct_chg").tolist(),
        "speed_values": _combined_speed(df).tolist(),
        "volume_ratio_values": finite_series(df, "volume_ratio").tolist(),
        "turnover_rate_values": finite_series(df, "turnover_rate").tolist(),
        "turnover_values": finite_series(df, "turnover").tolist(),
        "sixty_day_values": finite_series(df, "sixty_day_pct").tolist(),
        "ytd_values": finite_series(df, "ytd_pct").tolist(),
        "amplitude_values": finite_series(df, "amplitude").tolist(),
        "ret_3d_values": finite_series(df, "ret_3d").tolist(),
        "ret_5d_values": finite_series(df, "ret_5d").tolist(),
        "ret_10d_values": finite_series(df, "ret_10d").tolist(),
        "ret_20d_values": finite_series(df, "ret_20d").tolist(),
        "ma5_gap_values": finite_series(df, "ma5_gap").tolist(),
        "ma20_gap_values": finite_series(df, "ma20_gap").tolist(),
        "ma10_gap_values": finite_series(df, "ma10_gap").tolist(),
        "ma60_gap_values": finite_series(df, "ma60_gap").tolist(),
        "vol_ma5_ratio_values": finite_series(df, "vol_ma5_ratio").tolist(),
        "vol_amount_5d_values": finite_series(df, "vol_amount_5d").tolist(),
        "breakout_20d_values": finite_series(df, "breakout_20d").tolist(),
        "volatility_20d_values": finite_series(df, "volatility_20d").tolist(),
        "float_market_cap_values": finite_series(df, "float_market_cap").tolist(),
        "pe_dynamic_values": finite_series(df, "pe_dynamic").tolist(),
        "pb_values": finite_series(df, "pb").tolist(),
        "industry_values": list(industry_strength.values()),
    }


def _reversal_factor_correlation(context: Dict[str, List[float]]) -> Dict[str, float]:
    reversal_proxy = [-coerce_number(value) for value in context.get("ret_20d_values", [])]
    lowvol_proxy = [-coerce_number(value) for value in context.get("volatility_20d_values", [])]
    not_extended_proxy = [-coerce_number(value) for value in context.get("sixty_day_values", [])]
    return {
        "reversal_lowvol": _safe_corr(reversal_proxy, lowvol_proxy),
        "reversal_not_extended": _safe_corr(reversal_proxy, not_extended_proxy),
        "lowvol_not_extended": _safe_corr(lowvol_proxy, not_extended_proxy),
    }


def _safe_corr(left: List[float], right: List[float]) -> float:
    size = min(len(left), len(right))
    if size < 2:
        return 0.0
    a = pd.Series(left[:size], dtype="float64")
    b = pd.Series(right[:size], dtype="float64")
    if a.std() <= 1e-12 or b.std() <= 1e-12:
        return 0.0
    value = a.corr(b)
    return round(coerce_number(value), 4)


def _tomorrow_policy() -> Dict[str, object]:
    return {
        "main_max_gain": config.MAX_BUYABLE_GAIN_MAIN,
        "growth_max_gain": config.MAX_BUYABLE_GAIN_GROWTH,
        "min_turnover": config.MIN_TURNOVER,
        "avoid_limit_up": True,
        "risk_controls": ("高涨幅", "高量比", "高换手", "高振幅", "超涨damp硬门控"),
    }


def _horizon_meta(
    top_n: int,
    market_filter: str,
    candidate_count: int,
    strategy_version: str,
    strategy_label: str,
) -> Dict[str, object]:
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "candidate_count": candidate_count,
        "top_n": top_n,
        "market_filter": market_filter,
        "strategy_version": strategy_version,
        "strategy_label": strategy_label,
    }


def _horizon_row(row: pd.Series, scores: Dict[str, object]) -> Dict[str, object]:
    item = {
        "code": row["code"],
        "name": str(row.get("name", "")),
        "market": row.get("market", "main"),
        "market_label": config.MARKET_LABELS.get(row.get("market", "main"), "主板"),
        "industry": str(row.get("industry", "") or ""),
        "price": round(coerce_number(row.get("price")), 3),
        "pct_chg": round(coerce_number(row.get("pct_chg")), 2),
        "volume_ratio": round(coerce_number(row.get("volume_ratio")), 2),
        "turnover_rate": round(coerce_number(row.get("turnover_rate")), 2),
        "turnover": round(coerce_number(row.get("turnover")), 2),
        "sixty_day_pct": round(coerce_number(row.get("sixty_day_pct")), 2),
        "ytd_pct": round(coerce_number(row.get("ytd_pct")), 2),
        "amplitude": round(coerce_number(row.get("amplitude")), 2),
    }
    for key, value in scores.items():
        if key in ("reasons", "horizon", "theme", "breakout_20d"):
            item[key] = value
        elif isinstance(value, (int, float)):
            item[key] = round(max(0.0, min(100.0, value)), 2) if key == "score" else round(value, 2)
        else:
            item[key] = value
    return item


def _score_row(
    row: pd.Series,
    hot_ranks: Dict[str, int],
    industry_strength: Dict[str, float],
    sentiment_lookup: Dict[str, Dict[str, object]],
    context: Dict[str, List[float]],
    horizon: str,
    market_regime: Dict[str, object] = None,
) -> Dict[str, object]:
    code = row["code"]
    industry = str(row.get("industry", "") or "")
    pct_chg = coerce_number(row.get("pct_chg"))
    speed = _row_speed(row)
    volume_ratio = coerce_number(row.get("volume_ratio"))
    turnover_rate = coerce_number(row.get("turnover_rate"))
    turnover = coerce_number(row.get("turnover"))
    sixty_day_pct = coerce_number(row.get("sixty_day_pct"))
    ytd_pct = coerce_number(row.get("ytd_pct"))
    amplitude = coerce_number(row.get("amplitude"))
    ret_3d = coerce_number(row.get("ret_3d"))
    ret_5d = coerce_number(row.get("ret_5d"))
    ret_10d = coerce_number(row.get("ret_10d"))
    ret_20d = coerce_number(row.get("ret_20d"))
    ma5_gap = coerce_number(row.get("ma5_gap"))
    ma20_gap = coerce_number(row.get("ma20_gap"))
    vol_amount_5d = coerce_number(row.get("vol_amount_5d"))
    breakout_20d = coerce_number(row.get("breakout_20d"))
    volatility_20d = coerce_number(row.get("volatility_20d"))
    industry_pct = industry_strength.get(industry, 0.0)
    hot_rank = hot_ranks.get(code)
    sentiment = sentiment_lookup.get(code, {"score": 50.0, "summary": "未拉取到个股舆情"})
    execution_score = _execution_score(row)

    momentum_score = (
        percentile_score(pct_chg, context["pct_values"]) * 0.24
        + percentile_score(speed, context["speed_values"]) * 0.24
        + percentile_score(volume_ratio, context["volume_ratio_values"]) * 0.18
        + _optional_factor_score(ret_3d, context["ret_3d_values"]) * 0.12
        + _optional_factor_score(ret_5d, context["ret_5d_values"]) * 0.10
        + _optional_factor_score(vol_amount_5d, context["vol_amount_5d_values"]) * 0.08
        + _optional_factor_score(breakout_20d, context["breakout_20d_values"]) * 0.04
    )
    liquidity_score = (
        percentile_score(turnover_rate, context["turnover_rate_values"]) * 0.45
        + percentile_score(turnover, context["turnover_values"]) * 0.55
    )
    trend_score = (
        _optional_factor_score(ret_20d, context["ret_20d_values"], fallback=sixty_day_pct, fallback_values=context["sixty_day_values"]) * 0.24
        + percentile_score(sixty_day_pct, context["sixty_day_values"]) * 0.20
        + percentile_score(ytd_pct, context["ytd_values"]) * 0.14
        + _optional_factor_score(ma20_gap, context["ma20_gap_values"]) * 0.14
        + _optional_factor_score(ret_10d, context["ret_10d_values"]) * 0.12
        + _optional_factor_score(vol_amount_5d, context["vol_amount_5d_values"]) * 0.08
        + _optional_factor_score(
            volatility_20d,
            context["volatility_20d_values"],
            higher_is_better=False,
            fallback=amplitude,
            fallback_values=context["amplitude_values"],
        ) * 0.08
    )
    industry_score = (
        percentile_score(industry_pct, context["industry_values"]) if context["industry_values"] else 50.0
    )
    hot_score = _hot_rank_score(hot_rank)
    sentiment_score = coerce_number(sentiment.get("score"), 50.0)
    regime_style = "long" if horizon == "long" else "short"
    regime_bonus = _market_regime_adjustment(row, market_regime, regime_style)
    regime_profile = _regime_weight_profile(
        market_regime,
        ["trend", "liquidity", "momentum", "quality"] if horizon == "long" else ["momentum", "liquidity"],
    )

    if horizon == "long":
        strategy_name = "long_term"
        risk_penalty_parts = _long_term_risk_penalty_parts(row, sentiment)
        risk_penalty = _sum_penalty(risk_penalty_parts)
        reasons = _build_long_term_reasons(row, industry_pct, sentiment, trend_score, liquidity_score)
    else:
        strategy_name = "short_term"
        risk_penalty_parts = {}
        # 反转修正（默认关闭）：近期涨幅越高，按 reversal_tilt 比例减分。
        # 依据 A 股短线反转>动量证据；幅度由 calibrate --compare-momentum 回测决定。
        reversal_tilt = coerce_number(WEIGHTS["short_term"].get("reversal_tilt"), 0.0)
        if reversal_tilt > 0:
            recent_gain = coerce_number(row.get("ret_5d"), pct_chg)
            risk_penalty_parts["reversal_tilt"] = max(0.0, recent_gain) * reversal_tilt
        if sentiment.get("risk_words"):
            risk_penalty_parts["sentiment"] = 8
        if _near_limit_up_risk(row):
            risk_penalty_parts["near_limit_up"] = 5
        risk_penalty = _sum_penalty(risk_penalty_parts)
        reasons = _build_reasons(row, industry_pct, hot_rank, sentiment)
    combined = _combine_details(
        {
            "momentum_score": momentum_score,
            "liquidity_score": liquidity_score,
            "trend_score": trend_score,
            "industry_score": industry_score,
            "hot_score": hot_score,
            "sentiment_score": sentiment_score,
            "risk_penalty": risk_penalty,
            "regime_bonus": regime_bonus,
        },
        strategy_name,
        market_regime=market_regime,
        row=row,
    )
    final_score = combined["score"]

    item = {
        "code": code,
        "name": str(row.get("name", "")),
        "market": row.get("market", "main"),
        "market_label": config.MARKET_LABELS.get(row.get("market", "main"), "主板"),
        "industry": industry,
        "price": round(coerce_number(row.get("price")), 3),
        "pct_chg": round(pct_chg, 2),
        "speed": round(coerce_number(row.get("speed")), 2),
        "five_min_pct": round(coerce_number(row.get("five_min_pct")), 2),
        "volume_ratio": round(volume_ratio, 2),
        "turnover_rate": round(turnover_rate, 2),
        "turnover": round(turnover, 2),
        "industry_pct": round(industry_pct, 2),
        "sixty_day_pct": round(sixty_day_pct, 2),
        "ytd_pct": round(ytd_pct, 2),
        "ret_3d": round(ret_3d, 2),
        "ret_5d": round(ret_5d, 2),
        "ret_10d": round(ret_10d, 2),
        "ret_20d": round(ret_20d, 2),
        "ma5_gap": round(ma5_gap, 2),
        "ma20_gap": round(ma20_gap, 2),
        "vol_amount_5d": round(vol_amount_5d, 2),
        "breakout_20d": bool(breakout_20d),
        "volatility_20d": round(volatility_20d, 2),
        "hot_rank": hot_rank,
        "hot_score": round(hot_score, 2),
        "momentum_score": round(momentum_score, 2),
        "liquidity_score": round(liquidity_score, 2),
        "trend_score": round(trend_score, 2),
        "execution_score": round(execution_score, 2),
        "industry_score": round(industry_score, 2),
        "sentiment_score": round(sentiment_score, 2),
        "risk_penalty": round(risk_penalty, 2),
        "risk_penalty_parts": risk_penalty_parts,
        "regime_bonus": round(regime_bonus, 2),
        "regime_weight_profile": regime_profile,
        "base_score": round(combined["base_score"], 2),
        "raw_score": round(combined["raw_score"], 2),
        "overheat_damp": round(combined["overheat_damp"], 4),
        "score": round(max(0.0, min(100.0, final_score)), 2),
        "sentiment_summary": sentiment.get("summary", "暂无明显舆情信号"),
        "risk_words": sentiment.get("risk_words", []),
        "reasons": reasons,
        "horizon": horizon,
    }
    if horizon == "long":
        return _with_regime_reason(
            _attach_signal_explanation(item, row, "long_term", "长期推荐", "趋势稳健"),
            market_regime,
            regime_bonus,
        )
    return _with_regime_reason(
        _attach_signal_explanation(item, row, "short_term", "短线推荐", "盘中强势"),
        market_regime,
        regime_bonus,
    )


def _verdict_tier(score: float, risk_score: float, data_coverage: float) -> Dict[str, object]:
    """把裸 0-100 分映射成 verdict 评级阶梯（参考 UZI/Buffett 的离散评级）。

    50-65 的中性带按 risk 细分为 lean_bull / neutral / lean_bear；
    历史因子覆盖低于阈值时强制降级到 watch 并标注（A4 因子覆盖硬门控）。
    """
    t = THRESHOLDS["verdict"]
    score = max(0.0, min(100.0, coerce_number(score)))
    risk_score = max(0.0, min(100.0, coerce_number(risk_score)))
    low_coverage = data_coverage < THRESHOLDS["min_data_coverage"]

    if score >= t["strong_buy"] and risk_score < 60:
        tier, label = "strong_buy", "强烈关注"
    elif score >= t["buy"] and risk_score < 68:
        tier, label = "buy", "关注"
    elif score >= t["watch"]:
        if risk_score >= 70:
            tier, label = "reduce", "谨慎"
        elif score >= 60 and risk_score <= 48:
            tier, label = "watch", "观察(偏多)"
        elif score < 56 or risk_score >= 60:
            tier, label = "watch", "观察(偏空)"
        else:
            tier, label = "watch", "观察"
    elif score >= t["reduce"]:
        tier, label = "reduce", "谨慎"
    else:
        tier, label = "avoid", "回避"

    # 风控否决：风险极高直接压到回避，不让动量骑进前列（A5 硬淘汰路径）。
    if risk_score >= 80 and tier in ("strong_buy", "buy"):
        tier, label = "reduce", "谨慎"

    note = ""
    if low_coverage and tier in ("strong_buy", "buy"):
        tier, label, note = "watch", "观察(因子不足)", "历史因子覆盖不足，评级降级"
    elif low_coverage:
        note = "历史因子覆盖不足"

    return {
        "tier": tier,
        "label": label,
        "score": round(score, 2),
        "risk_score": round(risk_score, 2),
        "data_coverage": round(data_coverage, 2),
        "note": note,
    }


def _attach_signal_explanation(
    item: Dict[str, object],
    row: pd.Series,
    strategy_name: str,
    strategy_label: str,
    signal_label: str,
) -> Dict[str, object]:
    chase_risk = _chase_risk(row)
    overextension = _overextension_risk(row)
    failure_reasons = _failure_reasons(row, chase_risk, overextension)
    event_risk = row_event_risk(row)
    if event_risk.get("flags"):
        failure_reasons.extend("事件风险:{}".format(flag.get("label", "")) for flag in event_risk["flags"][:3])
    item.update(
        {
            "strategy_name": strategy_name,
            "strategy_label": strategy_label,
            "signal_label": signal_label,
            "chase_risk": chase_risk,
            "overextension": overextension,
            "failure_reasons": failure_reasons,
            "event_risk": event_risk,
        }
    )
    market_cap = coerce_number(row.get("market_cap"), None)
    if market_cap and market_cap > 0:
        item["market_cap"] = round(market_cap, 2)
    float_market_cap = coerce_number(row.get("float_market_cap"), None)
    if float_market_cap and float_market_cap > 0 and "float_market_cap" not in item:
        item["float_market_cap"] = round(float_market_cap, 2)
    item["agent_committee"] = _build_agent_committee(item, row)
    profile = _build_serenity_profile(item, row)
    item["serenity_profile"] = profile

    # A2：把牛熊双分提升到行顶层，便于前端双进度条直接读取，
    # 复用 agent_committee 已算好的 bull/bear（避免重复实现）。
    committee = item["agent_committee"]
    item["bull_score"] = round(coerce_number(committee.get("bull_researcher_score"), 50.0), 2)
    item["bear_score"] = round(coerce_number(committee.get("bear_researcher_score"), 50.0), 2)

    # A1 + A4：verdict 评级 + 数据覆盖硬门控。
    item["verdict"] = _verdict_tier(
        item.get("score"),
        profile.get("risk_score"),
        coerce_number(profile.get("data_coverage"), 0.0),
    )
    return item


def _build_agent_committee(item: Dict[str, object], row: pd.Series) -> Dict[str, object]:
    """Deterministic TradingAgents-style committee built from local signals."""
    chase_risk = item.get("chase_risk") or {}
    overextension = item.get("overextension") or {}
    risk_penalty = max(0.0, coerce_number(item.get("risk_penalty")))
    event_penalty = coerce_number((item.get("event_risk") or {}).get("penalty"))
    risk_penalty += event_penalty
    regime_bonus = coerce_number(item.get("regime_bonus"))
    risk_words = list(item.get("risk_words") or [])

    technical_score = _weighted_score(
        (
            (item.get("momentum_score"), 0.28),
            (item.get("trend_score"), 0.24),
            (item.get("execution_score"), 0.18),
            (item.get("early_trend_score"), 0.12),
            (item.get("not_overextended_score"), 0.10),
            (item.get("score"), 0.08),
        ),
        fallback=item.get("score"),
    )
    sentiment_score = max(0.0, min(100.0, coerce_number(item.get("sentiment_score"), 50.0) - len(risk_words) * 8.0))
    fundamentals_proxy_score = _weighted_score(
        (
            (item.get("quality_proxy_score"), 0.32),
            (item.get("industry_score"), 0.20),
            (item.get("theme_score"), 0.18),
            (item.get("liquidity_score"), 0.16),
            (item.get("not_overextended_score"), 0.14),
        ),
        fallback=item.get("score"),
    )
    news_environment_score = max(
        0.0,
        min(
            100.0,
            50.0
            + regime_bonus * 6.0
            - risk_penalty * 1.4
            - coerce_number(chase_risk.get("score")) * 4.0
            - coerce_number(overextension.get("score")) * 3.5
        ),
    )
    liquidity_score = _weighted_score(
        (
            (item.get("liquidity_score"), 0.76),
            (percentile_score(coerce_number(row.get("turnover")), [config.MIN_TURNOVER, config.MIN_TURNOVER * 4]), 0.24),
        ),
        fallback=50.0,
    )

    bull_score = _weighted_score(
        (
            (technical_score, 0.34),
            (fundamentals_proxy_score, 0.20),
            (sentiment_score, 0.16),
            (liquidity_score, 0.16),
            (news_environment_score, 0.14),
        ),
        fallback=item.get("score"),
    )
    bear_score = max(
        0.0,
        min(
            100.0,
            coerce_number(chase_risk.get("score")) * 11.5
            + coerce_number(overextension.get("score")) * 10.0
            + risk_penalty * 2.0
            + max(0.0, 50.0 - sentiment_score) * 0.55
            + max(0.0, 50.0 - news_environment_score) * 0.45
            + max(0.0, 55.0 - liquidity_score) * 0.25,
        ),
    )
    trader_score = max(0.0, min(100.0, bull_score * 0.62 + (100.0 - bear_score) * 0.28 + news_environment_score * 0.10))
    risk_score = min(100.0, bear_score + max(0.0, risk_penalty - 8.0) * 1.8)
    portfolio_score = max(
        0.0,
        min(
            100.0,
            trader_score * 0.68
            + liquidity_score * 0.14
            + fundamentals_proxy_score * 0.10
            + news_environment_score * 0.08
            - max(0.0, risk_score - 60.0) * 0.45,
        ),
    )

    if risk_score >= 78:
        action_label = "风控否决"
        stance = "reject"
    elif portfolio_score >= 72 and risk_score <= 48:
        action_label = "组合经理批准"
        stance = "approve"
    elif portfolio_score >= 60 and risk_score <= 62:
        action_label = "交易员小仓试单"
        stance = "small_position"
    else:
        action_label = "等待更多确认"
        stance = "wait"

    bull_cases = _agent_bull_cases(item, technical_score, fundamentals_proxy_score, sentiment_score, liquidity_score)
    bear_cases = _agent_bear_cases(item, risk_score, news_environment_score)
    return {
        "version": "trading_agents_committee_v1",
        "reference": TRADING_AGENTS_REFERENCE["repo"],
        "technical_analyst_score": round(technical_score, 2),
        "sentiment_analyst_score": round(sentiment_score, 2),
        "fundamentals_proxy_score": round(fundamentals_proxy_score, 2),
        "news_environment_score": round(news_environment_score, 2),
        "bull_researcher_score": round(bull_score, 2),
        "bear_researcher_score": round(bear_score, 2),
        "trader_score": round(trader_score, 2),
        "risk_manager_score": round(risk_score, 2),
        "portfolio_manager_score": round(portfolio_score, 2),
        "final_score": round(portfolio_score, 2),
        "final_action_label": action_label,
        "stance": stance,
        "bull_cases": bull_cases[:4],
        "bear_cases": bear_cases[:4],
        "source": "参考 TradingAgents 的分析师、研究辩论、交易员、风控和组合经理分层决策流；本项目使用本地量价/舆情/风险字段确定性计算。",
    }


def _weighted_score(pairs: Tuple[Tuple[object, float], ...], fallback: object = 50.0) -> float:
    total = 0.0
    weight_total = 0.0
    for value, weight in pairs:
        if value is None:
            continue
        num = coerce_number(value)
        if not pd.notna(num):
            continue
        total += max(0.0, min(100.0, num)) * weight
        weight_total += weight
    if weight_total <= 0:
        return max(0.0, min(100.0, coerce_number(fallback, 50.0)))
    return max(0.0, min(100.0, total / weight_total))


def _agent_bull_cases(
    item: Dict[str, object],
    technical_score: float,
    fundamentals_proxy_score: float,
    sentiment_score: float,
    liquidity_score: float,
) -> List[str]:
    cases: List[str] = []
    if technical_score >= 68:
        cases.append("技术分析师支持：趋势/动量组合较强")
    if fundamentals_proxy_score >= 62:
        cases.append("基本面代理支持：主题/行业/稳健代理分较好")
    if sentiment_score >= 60:
        cases.append("情绪分析师支持：舆情或热度偏正面")
    if liquidity_score >= 65:
        cases.append("交易员支持：流动性足，便于执行")
    if coerce_number(item.get("regime_bonus")) >= 2.5:
        cases.append("新闻环境支持：当前市场状态顺风")
    return cases or ["牛方暂无强证据"]


def _agent_bear_cases(item: Dict[str, object], risk_score: float, news_environment_score: float) -> List[str]:
    cases: List[str] = []
    cases.extend(str(reason) for reason in (item.get("failure_reasons") or [])[:3])
    if risk_score >= 65:
        cases.append("风控提示：综合风险分偏高")
    if news_environment_score <= 42:
        cases.append("新闻/市场环境偏逆风")
    if item.get("risk_words"):
        cases.append("情绪分析师提示：存在负面关键词")
    unique: List[str] = []
    for case in cases:
        if case and case not in unique:
            unique.append(case)
    return unique or ["熊方暂无硬性否决项"]


def _build_serenity_profile(item: Dict[str, object], row: pd.Series) -> Dict[str, object]:
    component_values = []
    evidence = []
    for key, label in PROFILE_COMPONENTS:
        if key not in item:
            continue
        value = coerce_number(item.get(key), 0.0)
        component_values.append(value)
        if value >= 72:
            evidence.append({"label": "{}强".format(label), "score": round(value, 2), "level": "positive"})
        elif value <= 38:
            evidence.append({"label": "{}弱".format(label), "score": round(value, 2), "level": "negative"})

    score = coerce_number(item.get("score"), 0.0)
    regime_bonus = coerce_number(item.get("regime_bonus"), 0.0)
    chase_risk = item.get("chase_risk") or {}
    overextension = item.get("overextension") or {}
    committee = item.get("agent_committee") or {}
    agent_score = coerce_number(committee.get("final_score"), 50.0)
    agent_risk_score = coerce_number(committee.get("risk_manager_score"), 0.0)
    risk_score = min(
        100.0,
        coerce_number(chase_risk.get("score")) * 11.0
        + coerce_number(overextension.get("score")) * 10.0
        + max(0.0, coerce_number(item.get("risk_penalty"))) * 2.1
        + coerce_number((item.get("event_risk") or {}).get("penalty")) * 1.5
        + max(0.0, -regime_bonus) * 4.0
        + max(0.0, agent_risk_score - 62.0) * 0.35,
    )
    data_coverage = _data_coverage(row)
    confidence_score = min(
        100.0,
        max(
            0.0,
            42.0
            + len([value for value in component_values if value >= 60]) * 7.0
            + data_coverage * 18.0
            + max(0.0, regime_bonus) * 1.6
            + max(0.0, agent_score - 55.0) * 0.22
            - risk_score * 0.18,
        ),
    )
    component_average = sum(component_values) / len(component_values) if component_values else score
    quality_score = min(
        100.0,
        max(
            0.0,
            score * 0.36
            + component_average * 0.25
            + confidence_score * 0.16
            + agent_score * 0.15
            - risk_score * 0.20,
        ),
    )
    committee_stance = committee.get("stance")
    if committee_stance == "reject" or risk_score >= 78:
        action_label = "只观察"
        level = "risk"
    elif quality_score >= 72 and risk_score <= 45 and agent_score >= 66:
        action_label = "优先跟踪"
        level = "good"
    elif risk_score >= 72:
        action_label = "只观察"
        level = "risk"
    elif quality_score >= 60 and agent_score >= 54:
        action_label = "小仓观察"
        level = "watch"
    else:
        action_label = "等待确认"
        level = "neutral"

    risk_reasons = list(chase_risk.get("reasons", [])) + list(overextension.get("reasons", []))
    risk_reasons.extend(committee.get("bear_cases", [])[:3])
    if regime_bonus <= -2.5:
        risk_reasons.append("市场状态逆风")
    event_risk = item.get("event_risk") or {}
    for flag in event_risk.get("flags", [])[:3]:
        risk_reasons.append("事件风险:{}".format(flag.get("label", "")))
    if regime_bonus >= 2.5:
        evidence.insert(0, {"label": "市场状态顺风", "score": round(regime_bonus, 2), "level": "positive"})
    if committee.get("final_action_label"):
        evidence.insert(
            0,
            {
                "label": "Agent委员会:{}".format(committee.get("final_action_label")),
                "score": round(agent_score, 2),
                "level": "positive" if committee_stance in ("approve", "small_position") else "negative",
            },
        )
    for case in committee.get("bull_cases", [])[:2]:
        evidence.append({"label": case, "score": round(agent_score, 2), "level": "positive"})

    return {
        "version": "serenity_profile_v1",
        "quality_score": round(quality_score, 2),
        "risk_score": round(risk_score, 2),
        "confidence_score": round(confidence_score, 2),
        "agent_committee_score": round(agent_score, 2),
        "data_coverage": round(data_coverage, 2),
        "level": level,
        "action_label": action_label,
        "evidence": evidence[:5],
        "risk_reasons": _unique_strings(risk_reasons)[:5],
        "source": "借鉴 Serenity 系列库的结构化证据与 TradingAgents 的多角色投研决策流。",
    }


def _data_coverage(row: pd.Series) -> float:
    explicit = row.get("alphalite_coverage")
    if explicit is not None:
        return max(0.0, min(1.0, coerce_number(explicit)))
    return 0.0


def _unique_strings(values: List[object]) -> List[str]:
    result: List[str] = []
    for value in values:
        text = str(value)
        if text and text not in result:
            result.append(text)
    return result


def _chase_risk(row: pd.Series) -> Dict[str, object]:
    pct = coerce_number(row.get("pct_chg"))
    market = row.get("market")
    upper = config.MAX_BUYABLE_GAIN_GROWTH if market in ("chinext", "star") else config.MAX_BUYABLE_GAIN_MAIN
    volume_ratio = coerce_number(row.get("volume_ratio"))
    turnover_rate = coerce_number(row.get("turnover_rate"))
    amplitude = coerce_number(row.get("amplitude"))
    reasons: List[str] = []
    score = 0
    if pct >= upper * 0.85:
        score += 3
        reasons.append("涨幅接近可买上限")
    elif pct >= upper * 0.70:
        score += 2
        reasons.append("当日涨幅偏高")
    if volume_ratio >= 5.5:
        score += 3
        reasons.append("量比过热")
    elif volume_ratio >= 4:
        score += 2
        reasons.append("量比偏高")
    if turnover_rate >= 18:
        score += 3
        reasons.append("换手过热")
    elif turnover_rate >= 12:
        score += 2
        reasons.append("换手偏高")
    if amplitude >= 12:
        score += 2
        reasons.append("振幅偏大")

    if score >= 5:
        level, label = "high", "高"
    elif score >= 2:
        level, label = "medium", "中"
    else:
        level, label = "low", "低"
    return {"level": level, "label": label, "score": score, "reasons": reasons}


def _overextension_risk(row: pd.Series) -> Dict[str, object]:
    sixty_day_pct = coerce_number(row.get("sixty_day_pct"))
    ytd_pct = coerce_number(row.get("ytd_pct"))
    ret_20d = coerce_number(row.get("ret_20d"))
    ma20_gap = coerce_number(row.get("ma20_gap"))
    reasons: List[str] = []
    score = 0
    if sixty_day_pct > 70:
        score += 3
        reasons.append("60日涨幅过大")
    elif sixty_day_pct > 45:
        score += 2
        reasons.append("60日涨幅偏大")
    if ytd_pct > 120:
        score += 3
        reasons.append("年内涨幅过大")
    elif ytd_pct > 80:
        score += 2
        reasons.append("年内涨幅偏大")
    if ret_20d > 45:
        score += 3
        reasons.append("20日涨幅过快")
    elif ret_20d > 25:
        score += 2
        reasons.append("20日涨幅偏快")
    if ma20_gap > 35:
        score += 3
        reasons.append("偏离20日线过远")
    elif ma20_gap > 22:
        score += 2
        reasons.append("偏离20日线偏远")

    if score >= 5:
        level, label = "high", "高"
    elif score >= 2:
        level, label = "medium", "中"
    else:
        level, label = "low", "低"
    return {"level": level, "label": label, "score": score, "reasons": reasons}


def _failure_reasons(
    row: pd.Series,
    chase_risk: Dict[str, object],
    overextension: Dict[str, object],
) -> List[str]:
    reasons: List[str] = []
    reasons.extend(str(reason) for reason in chase_risk.get("reasons", []))
    reasons.extend(str(reason) for reason in overextension.get("reasons", []))

    volume_ratio = coerce_number(row.get("volume_ratio"))
    turnover = coerce_number(row.get("turnover"))
    amplitude = coerce_number(row.get("amplitude"))
    pct = coerce_number(row.get("pct_chg"))
    if volume_ratio < 1:
        reasons.append("量能不足")
    if turnover < config.MIN_TURNOVER * 2:
        reasons.append("成交承接偏弱")
    if amplitude > 10:
        reasons.append("波动大，次日容易分歧")
    if pct < 0:
        reasons.append("当日走势偏弱")

    unique: List[str] = []
    for reason in reasons:
        if reason and reason not in unique:
            unique.append(reason)
    return unique[:6] or ["暂无明显单项风险，仍需次日走势验证"]


def _combined_speed(df: pd.DataFrame) -> pd.Series:
    speed = finite_series(df, "speed")
    five_min = finite_series(df, "five_min_pct")
    return speed.where(speed != 0, five_min)


def _row_speed(row: pd.Series) -> float:
    speed = coerce_number(row.get("speed"))
    if speed != 0:
        return speed
    return coerce_number(row.get("five_min_pct"))


def _hot_rank_score(rank) -> float:
    if not rank:
        return 50.0
    rank = int(rank)
    if rank <= 20:
        return 100.0
    if rank <= 50:
        return 88.0
    if rank <= 100:
        return 76.0
    if rank <= 200:
        return 62.0
    return 52.0


def _optional_factor_score(
    value: float,
    values: List[float],
    higher_is_better: bool = True,
    fallback: float = None,
    fallback_values: List[float] = None,
) -> float:
    if _has_signal(values):
        return percentile_score(value, values, higher_is_better=higher_is_better)
    if fallback is not None and fallback_values is not None:
        return percentile_score(fallback, fallback_values, higher_is_better=higher_is_better)
    return 50.0


def _has_signal(values: List[float]) -> bool:
    return any(abs(coerce_number(value)) > 1e-9 for value in values)


def _composite_score(parts: List[float]) -> float:
    clean = [max(0.0, min(100.0, coerce_number(value))) for value in parts if pd.notna(coerce_number(value))]
    if not clean:
        return 50.0
    return sum(clean) / len(clean)


def _near_limit_up_risk(row: pd.Series) -> bool:
    pct = coerce_number(row.get("pct_chg"))
    market = row.get("market")
    limit = 20 if market in ("chinext", "star") else 10
    turnover = coerce_number(row.get("turnover"))
    return pct >= limit * 0.88 and turnover < config.MIN_TURNOVER * 2


def _market_regime_adjustment(
    row: pd.Series,
    market_regime: Dict[str, object],
    strategy_style: str,
) -> float:
    if not market_regime:
        return 0.0

    level = market_regime.get("level")
    pct = coerce_number(row.get("pct_chg"))
    sixty_day_pct = coerce_number(row.get("sixty_day_pct"))
    amplitude = coerce_number(row.get("amplitude"))
    volume_ratio = coerce_number(row.get("volume_ratio"))
    turnover = coerce_number(row.get("turnover"))
    bonus = 0.0

    if level == "risk_on":
        if strategy_style in ("short", "tomorrow", "swing", "tech"):
            if pct > 0:
                bonus += 1.8
            if 1.1 <= volume_ratio <= 4.5:
                bonus += 1.6
            if turnover >= config.MIN_TURNOVER * 4:
                bonus += 1.2
        if strategy_style in ("long", "position") and sixty_day_pct >= 0:
            bonus += 0.8
        if amplitude > 11:
            bonus -= 1.5
    elif level == "risk_off":
        if strategy_style in ("short", "tomorrow", "tech"):
            if pct > 4:
                bonus -= 4.5
            if volume_ratio > 4.5:
                bonus -= 2.5
            if amplitude > 9:
                bonus -= 2.5
        if strategy_style in ("long", "position"):
            if 0 <= sixty_day_pct <= 40:
                bonus += 2.4
            if amplitude <= 7:
                bonus += 1.6
            if turnover >= config.MIN_TURNOVER * 3:
                bonus += 1.0
        if sixty_day_pct < -12:
            bonus -= 2.5
    else:
        if strategy_style in ("short", "tomorrow", "swing") and 1.0 <= volume_ratio <= 3.5:
            bonus += 0.8
        if amplitude > 12:
            bonus -= 1.2

    return round(bonus, 2)


def _regime_weight(key: str, market_regime: Dict[str, object], default: float = 1.0) -> float:
    if not market_regime:
        return default
    level = market_regime.get("level") or "balanced"
    profiles = WEIGHTS.get("regime_profiles") or {}
    profile = profiles.get(level) or profiles.get("balanced") or {}
    value = coerce_number(profile.get(key), default)
    return max(0.5, min(1.5, value))


def _regime_weight_profile(market_regime: Dict[str, object], keys: List[str]) -> Dict[str, float]:
    return {key: round(_regime_weight(key, market_regime), 3) for key in keys}


def _regime_component(score: float, key: str, market_regime: Dict[str, object]) -> float:
    """以 50 为中性点放大/压缩因子边际优势，避免把中性因子整体抬高。"""
    value = max(0.0, min(100.0, coerce_number(score, 50.0)))
    weight = _regime_weight(key, market_regime)
    return max(0.0, min(100.0, 50.0 + (value - 50.0) * weight))


def _regime_component_from_profile(score: float, key: str, profile: Dict[str, object]) -> float:
    value = max(0.0, min(100.0, coerce_number(score, 50.0)))
    weight = coerce_number((profile or {}).get(key), 1.0)
    weight = max(0.5, min(1.5, weight))
    return max(0.0, min(100.0, 50.0 + (value - 50.0) * weight))


def _combine(
    components: Dict[str, object],
    strategy: str,
    weights: Dict[str, object] = None,
    market_regime: Dict[str, object] = None,
    row: pd.Series = None,
    regime_weight_profile: Dict[str, object] = None,
) -> float:
    return _combine_details(
        components,
        strategy,
        weights=weights,
        market_regime=market_regime,
        row=row,
        regime_weight_profile=regime_weight_profile,
    )["score"]


def _combine_details(
    components: Dict[str, object],
    strategy: str,
    weights: Dict[str, object] = None,
    market_regime: Dict[str, object] = None,
    row: pd.Series = None,
    regime_weight_profile: Dict[str, object] = None,
) -> Dict[str, float]:
    spec = STRATEGY_COMBINERS.get(strategy)
    if not spec:
        raise KeyError("unknown strategy combiner: {}".format(strategy))
    all_weights = weights or WEIGHTS
    strategy_weights = all_weights.get(strategy, {})
    base = 0.0
    term_total = 0.0
    weighted_terms = []
    for term in spec["terms"]:
        key = term["component"]
        weight_key = term["weight_key"]
        weight = coerce_number(strategy_weights.get(weight_key), 0.0)
        if weight <= 0:
            continue
        value = coerce_number(components.get(key), 50.0)
        regime_key = term.get("regime_key")
        if regime_key:
            if regime_weight_profile:
                value = _regime_component_from_profile(value, regime_key, regime_weight_profile)
            else:
                value = _regime_component(value, regime_key, market_regime)
        weighted_terms.append((value, weight, weight * _factor_ic_multiplier(key)))
        term_total += weight
    adjusted_total = sum(item[2] for item in weighted_terms)
    scale = (term_total / adjusted_total) if adjusted_total > 1e-12 else 1.0
    for value, _, adjusted_weight in weighted_terms:
        base += value * adjusted_weight * scale
    if term_total <= 0:
        base = 0.0
    risk_penalty = coerce_number(components.get("risk_penalty"), 0.0)
    regime_bonus = coerce_number(components.get("regime_bonus"), 0.0)
    raw_score = base - risk_penalty + regime_bonus
    if spec.get("apply_damp"):
        if "overheat_damp" in components:
            damp = coerce_number(components.get("overheat_damp"), 1.0)
        elif row is not None:
            damp = _overheat_damp_multiplier(row)
        else:
            damp = 1.0
        damp = max(0.0, min(1.0, damp))
    else:
        damp = 1.0
    score = max(0.0, min(100.0, raw_score * damp))
    return {
        "score": score,
        "base_score": base,
        "raw_score": raw_score,
        "risk_penalty": risk_penalty,
        "regime_bonus": regime_bonus,
        "overheat_damp": damp,
    }


def _factor_ic_multiplier(component: str) -> float:
    if not getattr(config, "ENABLE_FACTOR_IC_WEIGHTING", False):
        return 1.0
    factor_key = COMPONENT_FACTOR_KEYS.get(component)
    if not factor_key:
        return 1.0
    payload = _factor_ic_payload()
    info = ((payload or {}).get("ic") or {}).get(factor_key) or {}
    if info.get("status") != "ok":
        return 1.0
    if int(info.get("sample_count") or 0) < int(getattr(config, "FACTOR_IC_MIN_SAMPLES", 30)):
        return 1.0
    band = max(0.0, min(0.8, coerce_number(getattr(config, "FACTOR_IC_WEIGHT_BAND", 0.3), 0.3)))
    ic = max(-1.0, min(1.0, coerce_number(info.get("ic"))))
    return max(0.1, 1.0 + max(-band, min(band, ic * band)))


def _factor_ic_payload() -> Dict[str, object]:
    path = getattr(config, "FACTOR_IC_PATH", ".runtime/factor_ic.json")
    try:
        mtime = os.path.getmtime(path)
    except Exception:
        _FACTOR_IC_CACHE["mtime"] = None
        _FACTOR_IC_CACHE["payload"] = {}
        return {}
    if _FACTOR_IC_CACHE.get("mtime") != mtime:
        _FACTOR_IC_CACHE["mtime"] = mtime
        _FACTOR_IC_CACHE["payload"] = load_factor_ic()
    return _FACTOR_IC_CACHE.get("payload") or {}


def _with_regime_reason(
    item: Dict[str, object],
    market_regime: Dict[str, object],
    regime_bonus: float,
) -> Dict[str, object]:
    if not market_regime:
        return item
    reasons = list(item.get("reasons", []))
    if regime_bonus >= 2.5:
        reasons.insert(0, "{}环境顺风".format(market_regime.get("label", "当前")))
    elif regime_bonus <= -2.5:
        reasons.append("{}环境下需谨慎".format(market_regime.get("label", "当前")))
    item["reasons"] = reasons[:6]
    return item


def _execution_score(row: pd.Series) -> float:
    pct = coerce_number(row.get("pct_chg"))
    market = row.get("market")
    upper = config.MAX_BUYABLE_GAIN_GROWTH if market in ("chinext", "star") else config.MAX_BUYABLE_GAIN_MAIN
    if pct <= 0:
        return 45.0
    if pct <= upper * 0.55:
        return 88.0
    if pct <= upper * 0.78:
        return 76.0
    return 58.0


def _tomorrow_risk_penalty(row: pd.Series) -> float:
    return _sum_penalty(_tomorrow_risk_penalty_parts(row))


def _sum_penalty(parts: Dict[str, float]) -> float:
    return round(sum(max(0.0, coerce_number(value)) for value in parts.values()), 2)


def _tomorrow_risk_penalty_parts(row: pd.Series) -> Dict[str, float]:
    penalty = 0.0
    pct = coerce_number(row.get("pct_chg"))
    market = row.get("market")
    upper = config.MAX_BUYABLE_GAIN_GROWTH if market in ("chinext", "star") else config.MAX_BUYABLE_GAIN_MAIN
    amplitude = coerce_number(row.get("amplitude"))
    turnover_rate = coerce_number(row.get("turnover_rate"))
    volume_ratio = coerce_number(row.get("volume_ratio"))
    parts = {}
    if pct >= upper * 0.85:
        parts["intraday_chase"] = 10
    elif pct >= upper * 0.72:
        parts["intraday_chase"] = 5
    if amplitude >= 12:
        parts["amplitude"] = 8
    if turnover_rate >= 18:
        parts["turnover_rate"] = 7
    elif turnover_rate >= 12:
        parts["turnover_rate"] = 3
    if volume_ratio >= 6:
        parts["volume_ratio"] = 8
    elif volume_ratio >= 4.5:
        parts["volume_ratio"] = 4
    return parts


def _apply_overheat_damp(final_score: float, row: pd.Series) -> float:
    """A5：过热乘法抑制。

    把 _not_overextended_score 折算成 [floor, 1.0] 的乘子作用在 final 上，
    让完全过热的票即使动量很高也无法骑进 top-N（加法权重做不到这一点）。
    """
    return final_score * _overheat_damp_multiplier(row)


def _overheat_damp_multiplier(row: pd.Series) -> float:
    not_overextended = _not_overextended_score(row) / 100.0
    floor = coerce_number(THRESHOLDS.get("overheat_damp_floor"), 0.6)
    return floor + (1.0 - floor) * max(0.0, min(1.0, not_overextended))


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


def _early_trend_score(row: pd.Series) -> float:
    sixty_day_pct = coerce_number(row.get("sixty_day_pct"))
    ytd_pct = coerce_number(row.get("ytd_pct"))
    pct = coerce_number(row.get("pct_chg"))
    score = 50.0
    if 3 <= sixty_day_pct <= 35:
        score += 24
    elif 0 <= sixty_day_pct < 3:
        score += 10
    elif 35 < sixty_day_pct <= 60:
        score += 8
    else:
        score -= 12
    if 0 <= ytd_pct <= 70:
        score += 16
    elif 70 < ytd_pct <= 100:
        score -= 8
    elif ytd_pct > 100:
        score -= 18
    if 0.5 <= pct <= 6:
        score += 10
    elif pct < -4 or pct > 9:
        score -= 10
    return max(0.0, min(100.0, score))


def _not_overextended_score(row: pd.Series) -> float:
    sixty_day_pct = coerce_number(row.get("sixty_day_pct"))
    ytd_pct = coerce_number(row.get("ytd_pct"))
    amplitude = coerce_number(row.get("amplitude"))
    score = 86.0
    if sixty_day_pct > 45:
        score -= min(30.0, (sixty_day_pct - 45) * 0.8)
    if ytd_pct > 80:
        score -= min(35.0, (ytd_pct - 80) * 0.6)
    if amplitude > 10:
        score -= 8
    if sixty_day_pct < -20:
        score -= 16
    return max(0.0, min(100.0, score))


def _balanced_volume_score(volume_ratio: float) -> float:
    if 1.2 <= volume_ratio <= 3.5:
        return 88.0
    if 0.8 <= volume_ratio < 1.2:
        return 68.0
    if 3.5 < volume_ratio <= 5.5:
        return 62.0
    if volume_ratio > 5.5:
        return 45.0
    return 50.0


def _tech_potential_risk_penalty(row: pd.Series) -> float:
    return _sum_penalty(_tech_potential_risk_penalty_parts(row))


def _tech_potential_risk_penalty_parts(row: pd.Series) -> Dict[str, float]:
    parts = dict(_tomorrow_risk_penalty_parts(row))
    pct = coerce_number(row.get("pct_chg"))
    if pct > 8:
        parts["intraday_chase_extra"] = 7
    return parts


def _swing_risk_penalty(row: pd.Series) -> float:
    return _sum_penalty(_swing_risk_penalty_parts(row))


def _swing_risk_penalty_parts(row: pd.Series) -> Dict[str, float]:
    pct = coerce_number(row.get("pct_chg"))
    volume_ratio = coerce_number(row.get("volume_ratio"))
    turnover_rate = coerce_number(row.get("turnover_rate"))
    volatility_20d = coerce_number(row.get("volatility_20d"))
    ma5_gap = coerce_number(row.get("ma5_gap"))
    parts = {}
    if pct > 7:
        parts["intraday_chase"] = 6
    if volume_ratio > 5.5:
        parts["volume_ratio"] = 7
    if turnover_rate > 18:
        parts["turnover_rate"] = 6
    if volatility_20d > 7:
        parts["volatility"] = 7
    if ma5_gap > 18:
        parts["ma5_gap"] = 5
    return parts


def _position_risk_penalty(row: pd.Series) -> float:
    return _sum_penalty(_position_risk_penalty_parts(row))


def _position_risk_penalty_parts(row: pd.Series) -> Dict[str, float]:
    pct = coerce_number(row.get("pct_chg"))
    amplitude = coerce_number(row.get("amplitude"))
    volatility_20d = coerce_number(row.get("volatility_20d"))
    ma20_gap = coerce_number(row.get("ma20_gap"))
    turnover = coerce_number(row.get("turnover"))
    parts = {}
    if pct > 5:
        parts["intraday_chase"] = 5
    if amplitude > 10 or volatility_20d > 6:
        parts["volatility"] = 8
    if ma20_gap > 30:
        parts["ma20_gap"] = 6
    if turnover < config.MIN_TURNOVER * 2:
        parts["liquidity"] = 5
    return parts


def _long_term_risk_penalty(row: pd.Series, sentiment: Dict[str, object]) -> float:
    return _sum_penalty(_long_term_risk_penalty_parts(row, sentiment))


def _long_term_risk_penalty_parts(row: pd.Series, sentiment: Dict[str, object]) -> Dict[str, float]:
    pct = coerce_number(row.get("pct_chg"))
    amplitude = coerce_number(row.get("amplitude"))
    ma20_gap = coerce_number(row.get("ma20_gap"))
    volatility_20d = coerce_number(row.get("volatility_20d"))
    turnover = coerce_number(row.get("turnover"))
    parts = {}
    if sentiment.get("risk_words"):
        parts["sentiment"] = 10
    if pct > 9:
        parts["intraday_chase"] = 6
    if ma20_gap > 35:
        parts["ma20_gap"] = 5
    if amplitude > 12 or volatility_20d > 6:
        parts["volatility"] = 5
    if turnover < config.MIN_TURNOVER * 2:
        parts["liquidity"] = 4
    return parts


def _build_reasons(
    row: pd.Series,
    industry_pct: float,
    hot_rank,
    sentiment: Dict[str, object],
) -> List[str]:
    reasons: List[str] = []
    pct = coerce_number(row.get("pct_chg"))
    speed = _row_speed(row)
    volume_ratio = coerce_number(row.get("volume_ratio"))
    turnover_rate = coerce_number(row.get("turnover_rate"))
    sentiment_score = coerce_number(sentiment.get("score"), 50)

    if pct >= 5:
        reasons.append("涨幅靠前")
    elif pct >= 2:
        reasons.append("涨幅稳步走强")
    if speed >= 1:
        reasons.append("短线涨速转强")
    if volume_ratio >= 2:
        reasons.append("量比明显放大")
    elif volume_ratio >= 1.3:
        reasons.append("量能温和放大")
    if turnover_rate >= 5:
        reasons.append("换手活跃")
    if industry_pct >= 1:
        reasons.append("所属行业偏强")
    if hot_rank and int(hot_rank) <= 100:
        reasons.append("市场人气靠前")
    if sentiment_score >= 65:
        reasons.append(str(sentiment.get("summary", "舆情偏正面")))
    elif sentiment.get("risk_words"):
        reasons.append(str(sentiment.get("summary", "命中风险舆情")))

    return reasons[:6] or ["综合动能和流动性排名靠前"]


def _build_long_term_reasons(
    row: pd.Series,
    industry_pct: float,
    sentiment: Dict[str, object],
    trend_score: float,
    liquidity_score: float,
) -> List[str]:
    reasons: List[str] = []
    sixty_day_pct = coerce_number(row.get("sixty_day_pct"))
    ytd_pct = coerce_number(row.get("ytd_pct"))
    turnover = coerce_number(row.get("turnover"))
    amplitude = coerce_number(row.get("amplitude"))
    ret_20d = coerce_number(row.get("ret_20d"))
    ma20_gap = coerce_number(row.get("ma20_gap"))
    vol_amount_5d = coerce_number(row.get("vol_amount_5d"))
    breakout_20d = coerce_number(row.get("breakout_20d"))
    volatility_20d = coerce_number(row.get("volatility_20d"))
    sentiment_score = coerce_number(sentiment.get("score"), 50)

    if trend_score >= 70:
        reasons.append("中期趋势排名靠前")
    if 5 <= sixty_day_pct <= 60:
        reasons.append("60日趋势稳健")
    if ret_20d >= 5:
        reasons.append("20日动量为正")
    if ma20_gap >= 0:
        reasons.append("站上20日均线")
    if ytd_pct >= 0:
        reasons.append("年内趋势为正")
    if liquidity_score >= 65 or turnover >= config.MIN_TURNOVER * 5:
        reasons.append("成交流动性较好")
    if vol_amount_5d >= 1.2:
        reasons.append("近5日成交额放大")
    if breakout_20d:
        reasons.append("接近20日突破")
    if industry_pct >= 0.8:
        reasons.append("行业趋势偏强")
    if amplitude <= 8 and volatility_20d <= 5:
        reasons.append("波动相对可控")
    if sentiment_score >= 60:
        reasons.append(str(sentiment.get("summary", "舆情偏正面")))
    if sentiment.get("risk_words"):
        reasons.append(str(sentiment.get("summary", "命中风险舆情")))

    return reasons[:6] or ["趋势、流动性和风险综合排名靠前"]


def _build_tomorrow_reasons(
    row: pd.Series,
    liquidity_score: float,
    momentum_score: float,
    trend_score: float,
    execution_score: float,
    risk_penalty: float,
) -> List[str]:
    reasons: List[str] = []
    pct = coerce_number(row.get("pct_chg"))
    volume_ratio = coerce_number(row.get("volume_ratio"))
    turnover_rate = coerce_number(row.get("turnover_rate"))
    turnover = coerce_number(row.get("turnover"))
    sixty_day_pct = coerce_number(row.get("sixty_day_pct"))
    amplitude = coerce_number(row.get("amplitude"))
    if liquidity_score >= 72 or turnover >= 500000000:
        reasons.append("成交额靠前")
    if 1.2 <= volume_ratio <= 4.5:
        reasons.append("量能放大但未过热")
    elif volume_ratio > 4.5:
        reasons.append("量能很强需防分歧")
    if 2 <= pct <= 7:
        reasons.append("涨幅可参与")
    elif pct > 7:
        reasons.append("强势但未触及涨停过滤")
    if turnover_rate >= 3:
        reasons.append("换手活跃")
    if trend_score >= 65 or sixty_day_pct >= 8:
        reasons.append("中期趋势向上")
    if execution_score >= 75:
        reasons.append("买入安全较好")
    if amplitude >= 9:
        reasons.append("波动偏大")
    if risk_penalty >= 8:
        reasons.append("风险扣分较高")
    if momentum_score >= 70:
        reasons.append("短线动能靠前")
    return reasons[:6] or ["流动性、动量和买入安全综合排名靠前"]


def _build_tech_potential_reasons(
    row: pd.Series,
    theme: str,
    early_trend_score: float,
    not_overextended_score: float,
    liquidity_score: float,
    risk_penalty: float,
) -> List[str]:
    reasons: List[str] = []
    pct = coerce_number(row.get("pct_chg"))
    sixty_day_pct = coerce_number(row.get("sixty_day_pct"))
    ytd_pct = coerce_number(row.get("ytd_pct"))
    volume_ratio = coerce_number(row.get("volume_ratio"))
    if theme:
        reasons.append(theme)
    if early_trend_score >= 70:
        reasons.append("趋势刚启动")
    elif 0 <= sixty_day_pct <= 35:
        reasons.append("60日涨幅未透支")
    if not_overextended_score >= 72:
        reasons.append("前期涨幅可控")
    if liquidity_score >= 65:
        reasons.append("流动性较好")
    if 1.1 <= volume_ratio <= 3.5:
        reasons.append("量能温和放大")
    if 0 <= ytd_pct <= 70:
        reasons.append("年内涨幅未过热")
    if 0.5 <= pct <= 6:
        reasons.append("当日涨幅可参与")
    if risk_penalty >= 10:
        reasons.append("高位风险扣分")
    return reasons[:6] or ["科技方向匹配且涨幅未明显透支"]


def _build_swing_reasons(
    row: pd.Series,
    momentum_score: float,
    trend_score: float,
    liquidity_score: float,
    risk_penalty: float,
) -> List[str]:
    reasons: List[str] = []
    ret_5d = coerce_number(row.get("ret_5d"))
    ret_10d = coerce_number(row.get("ret_10d"))
    ret_20d = coerce_number(row.get("ret_20d"))
    ma5_gap = coerce_number(row.get("ma5_gap"))
    ma20_gap = coerce_number(row.get("ma20_gap"))
    volume_ratio = coerce_number(row.get("volume_ratio"))
    vol_amount_5d = coerce_number(row.get("vol_amount_5d"))
    if momentum_score >= 68:
        reasons.append("5-10日动量靠前")
    if ret_5d > 0 or ret_10d > 0:
        reasons.append("短周期收益转强")
    if ret_20d > 0 or trend_score >= 65:
        reasons.append("20日趋势延续")
    if ma5_gap >= 0 or ma20_gap >= 0:
        reasons.append("站上关键均线")
    if 1.1 <= volume_ratio <= 4.0 or vol_amount_5d >= 1.1:
        reasons.append("量能温和配合")
    if liquidity_score >= 65:
        reasons.append("流动性较好")
    if risk_penalty >= 8:
        reasons.append("波段风险偏高")
    return reasons[:6] or ["波段动量、趋势和流动性综合靠前"]


def _build_position_reasons(
    row: pd.Series,
    theme: str,
    trend_score: float,
    quality_proxy_score: float,
    liquidity_score: float,
    risk_penalty: float,
) -> List[str]:
    reasons: List[str] = []
    sixty_day_pct = coerce_number(row.get("sixty_day_pct"))
    ytd_pct = coerce_number(row.get("ytd_pct"))
    ret_20d = coerce_number(row.get("ret_20d"))
    ma20_gap = coerce_number(row.get("ma20_gap"))
    volatility_20d = coerce_number(row.get("volatility_20d"))
    if theme and theme != "行业/趋势":
        reasons.append(theme)
    if trend_score >= 68:
        reasons.append("中期趋势靠前")
    if 0 <= sixty_day_pct <= 55:
        reasons.append("60日涨幅未过热")
    if 0 <= ytd_pct <= 90:
        reasons.append("年内趋势可控")
    if ret_20d > 0 or ma20_gap >= 0:
        reasons.append("20日趋势向上")
    if quality_proxy_score >= 70:
        reasons.append("涨幅和波动较均衡")
    if liquidity_score >= 65:
        reasons.append("成交承接较好")
    if volatility_20d <= 5:
        reasons.append("波动相对可控")
    if risk_penalty >= 9:
        reasons.append("中长期风险扣分")
    return reasons[:6] or ["中期趋势、流动性和风险控制综合靠前"]
