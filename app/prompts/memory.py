"""记忆提取 Prompt（法律域）。输出由 structured output schema 约束。"""

STM_EXTRACTION_PROMPT = """从下面的对话中提取"本会话内值得记住的关键事实"：
用户提到的案件编号与类型、关键日期（起诉/开庭/时效起算）、金额、对方当事人、明确的诉求。
只提取对话中明确出现的信息，不要推测。已有事实：{existing_facts}
已存在的事实不要重复提取；没有新事实就返回空列表。

对话记录：
{transcript}"""

LTM_EXTRACTION_PROMPT = """从下面的完整会话中提取值得跨会话记住的用户档案事实。
分类固定为：case(在办案件) / deadline(关键日期：开庭/时效届满) / asset(争议财产状况) /
history(既往咨询主题) / preference(沟通偏好) / basic(基本情况) / other。
只提取明确出现的信息；与已有档案重复的不要提取。同时用一句话概括本次会话主题。
已有档案：{existing_ltm}

会话摘要：{summary}

会话记录：
{transcript}"""
