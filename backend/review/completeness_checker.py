# =============================================================================
# 合同完整性检查 - completeness_checker.py
# =============================================================================
# 与"逐条审查"正交的整份合同体检，全部是确定性规则（零 LLM token）：
#   1. 必备条款清单：按合同类型倒查"没写出来的条款"——逐条审查只能审已写
#      条款，缺失的保密/知识产权/违约责任等条款永远发现不了；
#   2. 金额大小写校验：小写数字与中文大写金额是否一致；
#   3. 跨条款一致性：合同总额多处表述、分期付款加总、付款比例合计、
#      期限区间与期限描述、主体名称异写。
# 产出与 quality_issues 同构的条目（type/original/issue/suggestion/severity），
# 由 main.py 在任务级落库，Word 报告与前端独立成"合同完整性检查"区块展示。
# =============================================================================

import re
from datetime import date
from difflib import SequenceMatcher
from typing import List, Optional

# ---------------- 合同类型与必备条款 ----------------
# 判定方式：按各类型 detect 关键词的命中"种类数"打分，取最高分；
# 同分时靠前（更具体）的类型优先；全未命中则落到"通用合同"。
# required 元组：(条款名, 判定关键词, 缺失风险等级, 补充建议)
CONTRACT_PROFILES = [
    {
        "type": "技术服务/开发合同",
        "detect": ["技术开发", "技术服务", "技术委托", "技术咨询", "运维服务",
                   "软件开发", "系统集成", "技术支持", "开发合同"],
        "required": [
            ("服务内容与范围", ["服务内容", "服务范围", "工作内容", "开发内容", "项目内容", "服务事项"],
             "高", "建议明确服务内容、工作范围及交付物的具体边界"),
            ("合同价款与支付", ["合同价款", "合同金额", "服务费", "开发费用", "报酬", "支付方式"],
             "高", "建议明确合同总价、计价方式、付款节点及发票安排"),
            ("履行期限与交付", ["服务期限", "开发周期", "交付时间", "履行期限", "工期", "完成时间"],
             "中", "建议明确服务起止时间、关键里程碑与交付时间"),
            ("验收标准", ["验收"], "中", "建议约定验收标准、验收流程及未通过验收的处理方式"),
            ("违约责任", ["违约"], "高", "建议补充违约责任条款，明确违约情形、违约金标准及赔偿范围"),
            ("保密条款", ["保密"], "高", "建议补充保密条款，明确保密信息范围、保密期限与违约责任"),
            ("知识产权", ["知识产权", "著作权", "专利权", "成果归属", "技术成果"],
             "高", "建议明确履行合同产生的知识产权归属、使用许可与后续改进成果的权属"),
            ("争议解决", ["争议解决", "仲裁", "诉讼", "管辖"], "高",
             "建议补充争议解决条款，明确协商期限、管辖法院或仲裁机构"),
            ("不可抗力", ["不可抗力"], "低", "建议补充不可抗力条款，明确通知义务与责任免除范围"),
        ],
    },
    {
        "type": "采购/买卖合同",
        "detect": ["采购", "供货", "买卖", "销售", "订货", "购销"],
        "required": [
            ("标的与规格", ["标的", "货物名称", "产品名称", "规格型号", "品名"], "高",
             "建议明确货物名称、规格型号、数量及质量标准"),
            ("价款与支付", ["合同价款", "合同金额", "单价", "总价", "支付方式", "付款"], "高",
             "建议明确单价、总价、计价依据及付款节点"),
            ("交付与验收", ["交货", "交付", "验收"], "中",
             "建议明确交货时间、地点、运输方式及验收标准"),
            ("质量保证", ["质量保证", "质保", "质量标准", "三包"], "中",
             "建议明确质保期限、质保范围及售后响应方式"),
            ("违约责任", ["违约"], "高", "建议补充违约责任条款，明确逾期交货、质量不符的赔付标准"),
            ("争议解决", ["争议解决", "仲裁", "诉讼", "管辖"], "高",
             "建议补充争议解决条款，明确管辖法院或仲裁机构"),
            ("不可抗力", ["不可抗力"], "低", "建议补充不可抗力条款及通知义务"),
            ("通知送达", ["通知", "送达"], "低",
             "建议补充通知送达条款，明确联系人、地址、邮箱及送达生效规则"),
        ],
    },
    {
        "type": "租赁合同",
        "detect": ["租赁", "出租", "承租", "房租"],
        "required": [
            ("租赁物及用途", ["租赁物", "租赁房屋", "租赁标的", "房屋坐落", "用途"], "高",
             "建议明确租赁物位置、面积、权属状况及约定用途"),
            ("租金及支付", ["租金", "支付方式", "付款"], "高",
             "建议明确租金标准、支付周期、支付方式及逾期付款责任"),
            ("租赁期限", ["租赁期限", "租期", "租赁期"], "高",
             "建议明确租赁起止日期、续租条件及提前解约安排"),
            ("押金/保证金", ["押金", "保证金", "定金"], "中",
             "建议明确押金金额、退还条件与退还时限"),
            ("维修责任", ["维修", "修缮"], "中", "建议明确房屋及设施的维修责任划分"),
            ("违约责任", ["违约"], "高", "建议补充违约责任条款，明确逾期付租、擅自转租的后果"),
            ("争议解决", ["争议解决", "仲裁", "诉讼", "管辖"], "高",
             "建议补充争议解决条款，明确管辖法院或仲裁机构"),
        ],
    },
    {
        "type": "保密协议",
        "detect": ["保密协议", "保密合同", "NDA", "保密承诺"],
        "required": [
            ("保密信息定义", ["保密信息", "秘密信息", "保密内容"], "高",
             "建议明确保密信息的范围、载体与例外情形"),
            ("保密义务", ["保密义务", "不得披露", "不得向第三方", "保密责任"], "高",
             "建议明确接收方的保密义务、使用限制与人员管理要求"),
            ("保密期限", ["保密期限", "保密期"], "中",
             "建议明确保密义务的存续期限（含协议终止后的期限）"),
            ("违约责任", ["违约"], "高", "建议明确违反保密义务的违约金与损失赔偿计算方式"),
            ("争议解决", ["争议解决", "仲裁", "诉讼", "管辖"], "高",
             "建议补充争议解决条款，明确管辖法院或仲裁机构"),
        ],
    },
    {
        "type": "劳动/劳务合同",
        "detect": ["劳动合同", "劳务合同", "聘用", "劳动关系", "劳务关系"],
        "required": [
            ("工作内容与地点", ["工作内容", "工作岗位", "工作地点", "岗位"], "高",
             "建议明确工作岗位、工作内容及工作地点"),
            ("劳动报酬", ["劳动报酬", "工资", "薪酬", "报酬"], "高",
             "建议明确工资标准、支付周期及加班费计算方式"),
            ("工作时间与休息", ["工作时间", "休息休假", "工时"], "中",
             "建议明确工时制度、休息休假安排"),
            ("社会保险", ["社会保险", "社保", "五险"], "高",
             "建议明确社会保险的缴纳主体与缴纳基数"),
            ("合同期限", ["合同期限", "试用期", "聘用期"], "中",
             "建议明确合同期限、试用期及试用期工资"),
            ("解除与终止", ["解除", "终止"], "中",
             "建议明确解除/终止合同的条件、程序及经济补偿"),
            ("争议解决", ["争议解决", "仲裁", "诉讼"], "高",
             "建议约定劳动争议的解决途径（劳动仲裁前置）"),
        ],
    },
    {
        "type": "通用合同",
        "detect": [],
        "required": [
            ("合同主体", ["甲方", "乙方"], "高", "建议明确合同双方的主体名称、住所及联系方式"),
            ("标的与内容", ["标的", "服务内容", "工作内容", "货物", "项目内容"], "高",
             "建议明确合同标的、服务/货物内容及范围"),
            ("价款与支付", ["价款", "金额", "报酬", "支付", "付款"], "高",
             "建议明确合同价款、计价方式及付款节点"),
            ("履行期限", ["期限", "有效期", "履行期"], "中", "建议明确合同履行期限与关键节点"),
            ("违约责任", ["违约"], "高", "建议补充违约责任条款，明确违约情形与赔偿标准"),
            ("争议解决", ["争议解决", "仲裁", "诉讼", "管辖"], "高",
             "建议补充争议解决条款，明确管辖法院或仲裁机构"),
            ("不可抗力", ["不可抗力"], "低", "建议补充不可抗力条款及通知义务"),
            ("通知送达", ["通知", "送达"], "低",
             "建议补充通知送达条款，明确联系人、地址与送达生效规则"),
        ],
    },
]


# 已知合同类型（前端“确认合同类型”下拉与后端校验共用）
CONTRACT_TYPES = [profile["type"] for profile in CONTRACT_PROFILES]


def detect_contract_type(full_text: str) -> str:
    """按关键词命中种类数判定合同类型；无法判定时返回"通用合同"。"""
    best_type, best_hits = "通用合同", 0
    for profile in CONTRACT_PROFILES:
        hits = sum(1 for kw in profile["detect"] if kw in full_text)
        if hits > best_hits:
            best_type, best_hits = profile["type"], hits
    return best_type


def _profile_of(contract_type: str) -> dict:
    for profile in CONTRACT_PROFILES:
        if profile["type"] == contract_type:
            return profile
    return CONTRACT_PROFILES[-1]


def check_missing_clauses(full_text: str, contract_type: str,
                          required_clauses: Optional[List[dict]] = None) -> List[dict]:
    """按合同类型逐项检查必备条款是否"写到"，缺失项产出高/中/低提示。

    required_clauses 非空时优先级最高：审查方案（Playbook）可按企业口径
    覆盖内置 CONTRACT_PROFILES 的必备清单（例如甲方版增加"数据合规"）。
    """
    if required_clauses:
        required = [(r.get("name"), r.get("keywords") or [],
                     r.get("severity") or "中", r.get("suggestion") or "")
                    for r in required_clauses]
    else:
        required = _profile_of(contract_type)["required"]
    issues: List[dict] = []
    for name, keywords, severity, suggestion in required:
        # 关键词全部未出现 → 认定缺失（只看是否出现，不做语义判断）
        if any(kw in full_text for kw in keywords):
            continue
        issues.append({
            "type": "缺失条款",
            "original": "",
            "issue": f"合同中未发现「{name}」相关约定",
            "suggestion": suggestion,
            "severity": severity,
        })
    return issues


# ---------------- 金额大小写 ----------------
_CN_DIGITS = {"零": 0, "壹": 1, "贰": 2, "叁": 3, "肆": 4, "伍": 5,
              "陆": 6, "柒": 7, "捌": 8, "玖": 9,
              "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_SMALL_UNITS = {"拾": 10, "十": 10, "佰": 100, "百": 100, "仟": 1000, "千": 1000}
_CN_BIG_UNITS = {"万": 10000, "亿": 100000000}
_CAP_CHARS = "零壹贰叁肆伍陆柒捌玖拾佰仟万亿元圆角分整正"


def _cn_integer_value(text: str) -> Optional[int]:
    """把中文大写整数（壹佰贰拾万）转数值；含无法识别字符返回 None。"""
    total = section = number = 0
    seen = False
    for ch in text:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
            seen = True
        elif ch in _CN_SMALL_UNITS:
            # "拾贰" 的 拾 前面没有数字时按 1 个该单位计
            section += (number or 1) * _CN_SMALL_UNITS[ch]
            number = 0
            seen = True
        elif ch in _CN_BIG_UNITS:
            section += number
            if ch == "亿":
                total = (total + section) * _CN_BIG_UNITS[ch]
            else:
                total += section * _CN_BIG_UNITS[ch]
            section = number = 0
            seen = True
        else:
            return None
    if not seen:
        return None
    return total + section + number


def _cn_capital_to_value(text: str) -> Optional[float]:
    """把中文大写金额（壹佰贰拾万元整）转成元为单位的数值。"""
    s = re.sub(r"[人民币RMB￥¥\s：:（）()]", "", text or "")
    s = s.replace("圆", "元").replace("正", "整")
    if not s:
        return None
    if "元" in s:
        int_part, dec_part = s.split("元", 1)
    elif "角" in s or "分" in s:
        int_part, dec_part = "", s
    else:
        int_part, dec_part = s, ""
    dec_part = dec_part.replace("整", "")

    int_value = 0
    if int_part:
        parsed = _cn_integer_value(int_part)
        if parsed is None:
            return None
        int_value = parsed

    jiao = fen = 0
    m = re.search(r"([零壹贰叁肆伍陆柒捌玖])角", dec_part)
    if m:
        jiao = _CN_DIGITS[m.group(1)]
    m = re.search(r"([零壹贰叁肆伍陆柒捌玖])分", dec_part)
    if m:
        fen = _CN_DIGITS[m.group(1)]
    return int_value + jiao / 10 + fen / 100


_AMOUNT_NUM = r"(?:\d{1,3}(?:[,，]\d{3})+|\d+(?:\.\d+)?)"
# 形式一：小写在前，括号内大写（人民币120万元（大写：壹佰贰拾万元整））
_AMOUNT_PAIR_LOWER_FIRST = re.compile(
    r"((?:人民币|RMB)?\s*[¥￥]?\s*(?P<num>" + _AMOUNT_NUM + r")\s*"
    r"(?P<unit>亿元|万元|万|元)?)\s*[（(]\s*(?:人民币)?(?:大写)?\s*[：:]?\s*"
    r"(?P<cap>[" + _CAP_CHARS + r"]{2,})\s*[)）]")
# 形式二：大写在前，括号内小写（人民币壹佰贰拾万元整（￥1,200,000.00））
_AMOUNT_PAIR_CAPITAL_FIRST = re.compile(
    r"(?P<cap>[" + _CAP_CHARS + r"]{2,})\s*[（(]\s*(?:人民币|RMB)?\s*[¥￥]?\s*"
    r"(?P<num>" + _AMOUNT_NUM + r")\s*(?P<unit>亿元|万元|万|元)?\s*[)）]")


def _digit_amount_to_yuan(num_text: str, unit: str) -> Optional[float]:
    """小写金额 + 单位 → 元数值。"""
    try:
        value = float((num_text or "").replace(",", "").replace("，", ""))
    except ValueError:
        return None
    multiplier = {"亿元": 1e8, "万元": 1e4, "万": 1e4, "元": 1}.get(unit or "元", 1)
    return value * multiplier


def check_amount_capitalization(full_text: str) -> List[dict]:
    """校验"小写金额 + 中文大写"是否指向同一数值。

    只处理同时出现小写与大写的金额（大小写不一致是合同的高频致命笔误）；
    大写无法解析时静默跳过，避免规则误报。
    """
    issues: List[dict] = []
    for pattern in (_AMOUNT_PAIR_LOWER_FIRST, _AMOUNT_PAIR_CAPITAL_FIRST):
        for m in pattern.finditer(full_text):
            unit = m.group("unit") or "元"
            lower_value = _digit_amount_to_yuan(m.group("num"), unit)
            upper_value = _cn_capital_to_value(m.group("cap"))
            if lower_value is None or upper_value is None:
                continue
            if abs(lower_value - upper_value) <= 0.01:
                continue
            issues.append({
                "type": "数值冲突",
                "original": m.group(0)[:60],
                "issue": (f"金额大小写不一致：小写为 {m.group('num')}{unit}，"
                          f"大写为「{m.group('cap')}」（约 {upper_value:,.2f} 元）"),
                "suggestion": "核对正确金额，统一小写与大写表述（通常以大写金额为准）",
                "severity": "高",
            })
    return issues


# ---------------- 跨条款金额一致性 ----------------
# 仅捕获"明确的总价表述"，不抓裸"合计"——分期明细里的合计往往是分项小计
_TOTAL_AMOUNT_RE = re.compile(
    r"(?:合同(?:总)?(?:金额|价款|总价|总额|价)|总价款|总金额)\s*"
    r"(?:为|是|：|:)?\s*(?:人民币)?\s*[¥￥]?\s*"
    r"(?P<num>" + _AMOUNT_NUM + r")\s*(?P<unit>亿元|万元|万|元)")

_PAY_MARKERS = ("首付款", "首期", "预付款", "进度款", "验收款", "尾款",
                "质保金", "第一期", "第二期", "第三期", "第四期", "分期")
_PAY_SKIP_PREFIX = ("合同总", "总价款", "总金额", "总额", "合计", "总计", "共计")


def check_total_amount_consistency(full_text: str) -> List[dict]:
    """合同总金额在全文多处出现但数值不一致时报告。"""
    hits = []
    for m in _TOTAL_AMOUNT_RE.finditer(full_text):
        value = _digit_amount_to_yuan(m.group("num"), m.group("unit"))
        if value is not None:
            hits.append((round(value, 2), m.group(0)[:40]))
    values = sorted({v for v, _ in hits})
    if len(values) < 2:
        return []
    samples = "；".join(text for _, text in hits[:4])
    return [{
        "type": "数值冲突",
        "original": samples,
        "issue": "合同总金额存在多个不同表述：" + "、".join(f"{v:,.2f} 元" for v in values),
        "suggestion": "核对合同总金额，统一全文表述并确认大小写一致",
        "severity": "高",
    }]


def _clause_amounts(text: str) -> List[float]:
    """抽取条款内的金额数值（元），跳过"合同总价/合计"等总价表述。"""
    values = []
    for m in re.finditer(r"(?:人民币)?\s*[¥￥]?\s*(?P<num>" + _AMOUNT_NUM + r")\s*"
                         r"(?P<unit>亿元|万元|万|元)", text):
        prefix = text[max(0, m.start() - 8):m.start()]
        if any(k in prefix for k in _PAY_SKIP_PREFIX):
            continue
        value = _digit_amount_to_yuan(m.group("num"), m.group("unit"))
        if value is not None:
            values.append(value)
    return values


def check_installment_sum(clauses: List[dict], total_yuan: Optional[float]) -> List[dict]:
    """分期付款条款的各期金额加总与合同总额不符时报告（漏计期次/金额笔误）。"""
    if not total_yuan or total_yuan <= 0:
        return []
    issues: List[dict] = []
    for clause in clauses:
        text = clause.get("content") or ""
        if sum(1 for k in _PAY_MARKERS if k in text) < 2:
            continue
        amounts = _clause_amounts(text)
        if len(amounts) < 2:
            continue
        subtotal = sum(amounts)
        # 允许 1% 的四舍五入/尾差
        if abs(subtotal - total_yuan) <= max(total_yuan * 0.01, 1):
            continue
        issues.append({
            "type": "数值冲突",
            "original": f"分期金额合计约 {subtotal:,.2f} 元（合同总额 {total_yuan:,.2f} 元）",
            "issue": "分期付款各期金额加总与合同总额不一致，疑似漏计期次或金额笔误",
            "suggestion": "核对付款节点与各期金额，确保合计等于合同总价并明确尾差处理",
            "severity": "中",
        })
        if len(issues) >= 2:
            break
    return issues


def check_payment_percent_sum(clauses: List[dict]) -> List[dict]:
    """付款比例合计不为 100% 时提示（只在付款语义的句子里统计，排除违约/税费句）。"""
    issues: List[dict] = []
    for clause in clauses:
        text = clause.get("content") or ""
        if not any(k in text for k in ("支付", "付款", "预付", "结算")):
            continue
        percents: List[float] = []
        for seg in re.split(r"[。；\n]", text):
            if not any(k in seg for k in ("支付", "付款", "预付", "结算")):
                continue
            if any(k in seg for k in ("违约", "逾期", "滞纳", "赔偿", "罚", "税")):
                continue
            percents += [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*[%％]", seg)]
        if len(percents) < 2:
            continue
        total = sum(percents)
        if 99.5 <= total <= 100.5:
            continue
        issues.append({
            "type": "数值冲突",
            "original": "付款比例：" + " + ".join(f"{p:g}%" for p in percents) + f" = {total:g}%",
            "issue": f"付款比例合计为 {total:g}%，未覆盖合同全部价款",
            "suggestion": "核对各期付款比例，确保合计为 100%（含质保金）",
            "severity": "中" if total < 90 else "低",
        })
        if len(issues) >= 2:
            break
    return issues


# ---------------- 期限一致性 ----------------
_DATE_RANGE_RE = re.compile(
    r"(?P<y1>20\d{2})\s*年\s*(?P<m1>\d{1,2})\s*月\s*(?P<d1>\d{1,2})\s*日\s*(?:起)?\s*"
    r"(?:至|到|—|－|-|~|～)\s*"
    r"(?P<y2>20\d{2})\s*年\s*(?P<m2>\d{1,2})\s*月\s*(?P<d2>\d{1,2})\s*日")
_DURATION_RE = re.compile(
    r"(?:合同|服务|租赁|合作|有效|履行)?(?:期限|有效期|服务期|租期|合同期)\s*"
    r"(?:为|是|共|共计)?\s*(?P<num>[0-9一二三四五六七八九十百两]+)\s*(?P<unit>年|个月|月|日|天)")


def _cn_number(text: str) -> Optional[int]:
    """解析阿拉伯数字或简单中文数字（十二 / 二十四 / 3）。"""
    if text.isdigit():
        return int(text)
    total = number = 0
    for ch in text:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
        elif ch == "十":
            total += (number or 1) * 10
            number = 0
        elif ch == "百":
            total += (number or 1) * 100
            number = 0
        else:
            return None
    return total + number


def _safe_date(y, m, d) -> Optional[date]:
    try:
        return date(int(y), int(m), int(d))
    except ValueError:
        return None


def check_term_consistency(full_text: str) -> List[dict]:
    """期限区间（起止日期）与"期限为 X 年/个月"描述互相矛盾时报告。"""
    issues: List[dict] = []
    ranges = []
    for m in _DATE_RANGE_RE.finditer(full_text):
        start = _safe_date(m.group("y1"), m.group("m1"), m.group("d1"))
        end = _safe_date(m.group("y2"), m.group("m2"), m.group("d2"))
        if not start or not end:
            continue
        if end <= start:
            issues.append({
                "type": "期限矛盾",
                "original": m.group(0)[:40],
                "issue": "合同期限的结束日期不晚于开始日期",
                "suggestion": "核对合同起止日期，修正错误的年份或月份",
                "severity": "高",
            })
            continue
        ranges.append((start, end))

    if len(ranges) == 1:
        start, end = ranges[0]
        days = (end - start).days
        m = _DURATION_RE.search(full_text)
        if m:
            num = _cn_number(m.group("num"))
            unit = m.group("unit")
            if num:
                expected = num * {"年": 365, "个月": 30, "月": 30, "日": 1, "天": 1}[unit]
                # 合同期限常有"生效日≠起始日"的编排差异，留 45 天容差
                if abs(days - expected) > 45:
                    issues.append({
                        "type": "期限矛盾",
                        "original": m.group(0)[:40],
                        "issue": (f"约定期限为 {m.group('num')}{unit}，"
                                  f"但起止日期跨度为 {days} 天，两者不一致"),
                        "suggestion": "核对合同期限描述与起止日期，统一期限口径",
                        "severity": "中",
                    })
    return issues


# ---------------- 主体名称一致性 ----------------
_ORG_RE = re.compile(
    r"([\u4e00-\u9fa5A-Za-z0-9]{2,30}?(?:有限责任公司|股份有限公司|有限公司|公司|集团|事务所|研究院|学院|大学))")


_ORG_SUFFIXES = ("股份有限公司", "有限责任公司", "有限公司", "公司",
                 "集团", "事务所", "研究院", "学院", "大学")


def _core_name(name: str) -> str:
    """去掉括号备注/引号/空白，得到主体名称主干用于比对。"""
    clean = re.sub(r"[（(].*?[)）]", "", name or "")
    return re.sub(r"[\s“”\"']", "", clean)


def _name_variants(name: str) -> set:
    """由主体全称生成常见的名称异写（简称/换后缀），用于正文命中比对。"""
    variants = set()
    for suffix in _ORG_SUFFIXES:
        if name.endswith(suffix):
            core = name[:-len(suffix)]
            variants.update({core + "公司", core + "有限公司", core})
            break
    variants.discard(name)
    return {v for v in variants if len(v) >= 4}


def check_party_consistency(full_text: str, meta: dict) -> List[dict]:
    """正文中出现与首部主体不一致的名称时报告（简称混用/笔误/主体混淆）。"""
    parties = [_core_name(meta.get(k) or "") for k in ("party_a", "party_b")]
    parties = [p for p in parties if p]
    if not parties:
        return []

    suspicious = []
    # 路径一：由全称推导简称/换后缀异写（如"XX科技有限公司"→"XX科技公司"），直接全文命中。
    # 先抹掉全称本身，避免"全称包含简称核心"造成的自我命中
    scrubbed = full_text
    for party in parties:
        scrubbed = scrubbed.replace(party, "")
    for party in parties:
        for variant in _name_variants(party):
            if variant in scrubbed:
                suspicious.append((variant, party))

    # 路径二：正文公司名与首部主体高度相似但不互为子串（错字/漏字/主体混淆），
    # 互为子串的情况（缩写/带上下文前缀）交给路径一，避免重复与误报
    for m in _ORG_RE.finditer(full_text):
        candidate = _core_name(m.group(1))
        if not candidate or candidate in parties:
            continue
        matched = max(parties, key=lambda p: SequenceMatcher(None, candidate, p).ratio())
        ratio = SequenceMatcher(None, candidate, matched).ratio()
        if ratio >= 0.9 and candidate not in matched and matched not in candidate:
            suspicious.append((candidate, matched))

    issues: List[dict] = []
    seen = set()
    for candidate, matched in suspicious:
        key = (candidate, matched)
        if key in seen:
            continue
        # 同一主体的多个异写只保留最长的：短异写往往只是长异写的前缀
        if any(candidate != c2 and candidate in c2 and matched == m2
               for c2, m2 in suspicious):
            continue
        seen.add(key)
        issues.append({
            "type": "主体不一致",
            "original": f"{candidate} / {matched}",
            "issue": f"正文出现主体名称「{candidate}」，与首部主体「{matched}」不一致",
            "suggestion": "核对当事人名称，统一使用营业执照全称，避免主体混用",
            "severity": "中",
        })
        if len(issues) >= 2:
            break
    return issues


# ---------------- 统一入口 ----------------
_SEVERITY_ORDER = {"高": 0, "中": 1, "低": 2}


def analyze_contract(full_text: str, clauses: List[dict],
                     meta: Optional[dict] = None,
                     required_clauses: Optional[List[dict]] = None) -> dict:
    """整份合同体检入口。

    返回 {"contract_type": 类型, "issues": [...]}；issues 与 quality_issues
    同构，由调用方按任务级落库（不挂到单条条款上——缺失条款没有对应条款）。
    required_clauses 来自审查方案快照，为空时回退内置类型清单。
    任一子检查异常都只跳过该项，绝不影响审查主流程。
    """
    meta = meta or {}
    contract_type = detect_contract_type(full_text)

    issues: List[dict] = []

    def _safe(fn, *args):
        try:
            issues.extend(fn(*args) or [])
        except Exception as e:
            print(f"[Completeness] {getattr(fn, '__name__', fn)} 检查降级: {e}")

    _safe(check_missing_clauses, full_text, contract_type, required_clauses)
    _safe(check_amount_capitalization, full_text)
    _safe(check_total_amount_consistency, full_text)
    _safe(check_installment_sum, clauses, _parse_total_yuan(meta.get("total_amount")))
    _safe(check_payment_percent_sum, clauses)
    _safe(check_term_consistency, full_text)
    _safe(check_party_consistency, full_text, meta)

    # 去重（同类型同描述）后按严重度排序，控制总量避免报告刷屏
    unique = []
    seen = set()
    for item in issues:
        key = (item.get("type"), item.get("issue"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    unique.sort(key=lambda x: _SEVERITY_ORDER.get(x.get("severity"), 3))
    return {"contract_type": contract_type, "issues": unique[:30]}


def _parse_total_yuan(total_amount: Optional[str]) -> Optional[float]:
    """把元信息里的合同总额（"500万元"/"1,200,000元"）转成元数值。"""
    if not total_amount:
        return None
    m = re.match(r"^\s*([\d,，]+(?:\.\d+)?)\s*(亿元|万元|万|元)?\s*$", total_amount)
    if not m:
        return None
    return _digit_amount_to_yuan(m.group(1), m.group(2) or "元")
