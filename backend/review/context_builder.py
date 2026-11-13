# =============================================================================
# 上下文组装 - context_builder.py
# =============================================================================
# 将"合同公共信息 + 企业风控规则 + 待审查条款 + RAG 检索法条"组装为模型输入。
# 组装逻辑集中在此，便于后续对照微调数据样本（PRD 附录 10.1）调整 Prompt。
# =============================================================================

from typing import List, Optional

from config.prompts import REVIEW_SYSTEM_PROMPT, QUALITY_ONLY_SYSTEM_PROMPT
from review.injection_guard import ISOLATION_NOTE, wrap_untrusted

# 字段名与 LLM 输出结构约定（verdict_parser.parse_verdict 同步约束）
VERDICT_SCHEMA = """必须输出一个合法 JSON 对象（不要任何解释文字），字段如下：
{
  "risk_level": "高|中|低|无",
  "risk_description": "风险描述：条款内容 + 判断依据",
  "law_basis": "法条依据原文，无引用时填空字符串",
  "suggestion": "具体修改建议，无风险时写'无需修改'",
  "confidence": 0.0 到 1.0 的数字,
  "quality_issues": [
    {"type": "错别字|语义歧义|表述空泛|术语不一致|数值冲突|格式异常|指代不明|其他",
     "original": "原文片段", "issue": "问题说明", "suggestion": "修改建议", "severity": "高|中|低"}
  ],
  "reasonableness_issues": [
    {"type": "权利义务失衡|责任不对等|付款交付不匹配|条款不可执行|期限不合理|其他",
     "direction": "对甲方不利|对乙方不利|双方失衡",
     "original": "原文片段", "issue": "为什么不合理", "suggestion": "修改建议", "severity": "高|中|低"}
  ],
  "revised_text": "改写后的完整条款文本；无需修改时填空字符串"
}"""


def build_quality_check_messages(clause: dict, risk_hint: str = "",
                                 plan_text: str = "", stance: str = "甲方") -> List[dict]:
    """构造"文本质量 + 合理性 + 修订文本"体检 messages（规则已命中风险时使用）。

    只喂条款正文，不带风控规则和法条——避免模型重复判风险或推翻规则结论，
    也让这一步的 token 消耗降到最低。risk_hint 仅把规则已判定的风险描述带入，
    让模型在改写 revised_text 时能针对性修复；plan_text 为审查方案的附加要求，
    保证规则通道命中时改写同样遵循方案口径；stance 用于纠偏质量体检里的
    商业合理性判断（QUALITY_ONLY 提示词默认甲方视角）。
    """
    clause_block = wrap_untrusted(
        f"{clause.get('clause_id')} {clause.get('title') or ''}\n"
        f"{clause.get('content')}")
    hint_block = f"\n【规则引擎已判定】\n{risk_hint}\n" if risk_hint else ""
    plan_block = f"\n【审查方案要求】\n{plan_text}\n" if plan_text else ""
    if stance == "中立":
        stance_block = ("\n【审查立场】本次为中立审查：请同时识别对甲方、乙方不利"
                        "或双方失衡的商业安排，direction 如实填写。\n")
    elif stance and stance != "甲方":
        stance_block = (f"\n【审查立场】本次审查立场为{stance}：商业合理性请以{stance}的"
                        f"利益为核心判断，direction 使用“对{stance}不利”。\n")
    else:
        stance_block = ""
    return [
        {"role": "system", "content": QUALITY_ONLY_SYSTEM_PROMPT + ISOLATION_NOTE},
        {"role": "user",
         "content": f"【待校对条款】\n{clause_block}{plan_block}{stance_block}{hint_block}"},
    ]


def _plan_block(contract_meta: dict) -> str:
    """渲染审查方案信息段（方案名/立场/附加要求），未配置方案时返回空串。"""
    name = contract_meta.get("plan_name")
    stance = contract_meta.get("stance")
    extra = contract_meta.get("extra_prompt")
    if not (name or extra):
        return ""
    lines = ["【审查方案】"]
    if name:
        lines.append(f"方案名称：{name}")
    if stance:
        lines.append(f"审查立场：{stance}")
    if extra:
        lines.append(f"附加审查要求：{extra}")
    return "\n".join(lines) + "\n\n"


def _stance_system_note(stance: Optional[str]) -> str:
    """非甲方立场时在系统提示词后追加覆盖说明（主提示词默认甲方视角）。"""
    if not stance or stance == "甲方":
        return ""
    return (f"\n\n## 本次审查立场\n本次审查方案指定的立场为「{stance}」："
            f"请以{stance}的利益为核心判断风险等级与商业合理性；"
            f"上述甲方视角的示例仅作格式参考，不得据此判定立场。")


MISSING_CLAUSE_SYSTEM_PROMPT = """你是企业法务，负责为审查中发现的“缺失条款”起草可直接并入合同的条款文本。

要求：
1. 只输出条款正文，不要解释、不要 markdown、不要输出“如下”之类的话；
2. 条款编号用【】占位（如【第X条】），便于法务插入时统一编号；
3. 结合给定的合同类型、我方立场与审查建议，权利义务要具体、可执行；
4. 金额、比例、期限等关键数值若无依据，用“【待填写】”占位，不得编造；
5. 可以引用法律名称，但不要编造具体条号。"""


def build_missing_clause_messages(contract_meta: dict, clause_name: str,
                                  suggestion: str, law_context: str = "",
                                  stance: str = "甲方") -> List[dict]:
    """构造“缺失条款草案”提示词：输出一段可并入合同的条款正文。"""
    meta_block = (
        f"合同名称：{contract_meta.get('contract_name') or '-'}\n"
        f"合同类型：{contract_meta.get('contract_type') or '-'}\n"
        f"甲方：{contract_meta.get('party_a') or '-'}\n"
        f"乙方：{contract_meta.get('party_b') or '-'}\n"
        f"合同金额：{contract_meta.get('total_amount') or '-'}\n"
        f"审查立场：{stance}"
    )
    law_block = f"\n【可参考的法律材料】\n{law_context[:1200]}\n" if law_context else ""
    user_content = (
        f"【合同信息】\n{meta_block}\n\n"
        f"【需补充的条款】{clause_name}\n"
        f"【审查建议】{suggestion or '按企业标准口径补充'}\n"
        f"{law_block}\n"
        f"请以{stance}利益为核心起草该条款正文。"
    )
    return [
        {"role": "system", "content": MISSING_CLAUSE_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def _rules_text(rules: List[dict]) -> str:
    """把启用的企业风控规则渲染为文本段，随 Prompt 交给 LLM 作为须遵守的审查红线。"""
    if not rules:
        return "（未启用额外企业风控规则）"
    lines = []
    for r in rules:
        lines.append(f"- {r.get('name')}（风险等级：{r.get('risk_level')}）: "
                     f"{r.get('description', '')}")
    return "\n".join(lines)


def build_review_messages(contract_meta: dict, clause: dict, law_context: str,
                          rules: List[dict], fewshot_block: str = "") -> List[dict]:
    """构造逐条审查的 chat messages（system + user）。

    fewshot_block 为企业历史采纳改写的先例片段（见 review/fewshot.py）；
    为空时不占 token，保持无先例时的行为与之前完全一致。
    """
    meta_lines = [
        f"合同名称：{contract_meta.get('contract_name') or '未知'}",
        f"甲方：{contract_meta.get('party_a') or '未知'}",
        f"乙方：{contract_meta.get('party_b') or '未知'}",
        f"合同金额：{contract_meta.get('total_amount') or '未知'}",
    ]
    meta_block = "\n".join(meta_lines)

    clause_header = clause.get("title") or clause.get("clause_id")
    clause_block = wrap_untrusted(
        f"{clause.get('clause_id')} {clause_header}\n"
        f"{clause.get('content')}")

    if law_context:
        law_block = f"【检索到的相关法律条文】\n{law_context}\n"
        law_note = ("引用上述法条时需忠实原文；上述材料之外的任何法条引用均不被允许。")
    else:
        law_block = "【检索到的相关法律条文】\n（未检索到相关法条）\n"
        law_note = "由于未提供法条材料，判断时仅能依据一般法律常识，且 law_basis 必须填空字符串，禁止编造法条。"
    law_block += law_note

    user_content = (
        f"{law_block}\n\n"
        f"{_plan_block(contract_meta)}"
        f"【企业风控规则】\n{_rules_text(rules)}\n\n"
        f"{fewshot_block}"
        f"【合同公共信息】\n{meta_block}\n\n"
        f"【待审查条款】\n{clause_block}\n\n"
        f"{VERDICT_SCHEMA}"
    )
    return [
        {"role": "system",
         "content": REVIEW_SYSTEM_PROMPT + _stance_system_note(contract_meta.get("stance"))
         + ISOLATION_NOTE},
        {"role": "user", "content": user_content},
    ]


def retry_note() -> str:
    """解析失败后的附加纠正提示（追加为一条 user 消息）。"""
    return "你上次的输出不是合法 JSON，请只输出一个合法的 JSON 对象，不要包含任何其他文字或代码块标记。"
