"""CLI 入口：python main.py [--mock] [--multi] [--trace]

命令：quit/exit 退出 | reset 重置会话 | skills 查看技能 | memory 查看记忆
"""

from __future__ import annotations

import sys

from app.config.settings import Settings
from app.schemas.response import INTENT_LABELS, IntentType, UrgencyLevel

URGENCY_LABELS = {"routine": "常规", "attention": "留意", "urgent": "紧急",
                  "emergency": "急症"}


def main() -> None:
    args = sys.argv[1:]
    settings = Settings()
    if "--mock" in args:
        settings.mock_mode = True
    if "--multi" in args:
        settings.multi_agent_enabled = True

    if settings.multi_agent_enabled:
        from app.multi_agent.orchestrator import MultiAgentOrchestrator
        agent = MultiAgentOrchestrator(settings)
        mode = "Multi-Agent（婚姻家事/合同债务/侵权赔偿/紧急通道 分流）"
    else:
        from app.agent.chat import LegalAgent
        agent = LegalAgent(settings)
        mode = "单 Agent"
    agent.trace_enabled = "--trace" in args

    offline = settings.mock_mode or not settings.openai_api_key
    print("=" * 56)
    print(f"  法律服务 · 咨询助手「小律」({mode})")
    print(f"  模式: {'离线 mock' if offline else settings.model_name} | "
          f"工具/知识库/记忆/技能 已装配")
    print("  试试: 查案件 LS-2026-001 / 民间借贷利率上限 / 朋友欠钱2023年5月借的时效还剩多久")
    print("        离婚孩子抚养权 / 车祸赔偿哪些项目 / 前男友威胁我要曝光照片")
    print("  命令: quit退出 · reset重置 · skills技能 · memory记忆")
    if agent.trace_enabled:
        print("  TRACE: 显示模型输入/响应、工具调用/结果与结构化处理（不含模型私有思维链）")
    print("=" * 56)
    if agent.history_size:
        print(f"💬 已恢复上次会话（{agent.history_size} 条历史）")

    while True:
        try:
            user_input = input("\n👤 你: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not user_input:
            continue
        low = user_input.lower()
        if low in ("quit", "exit"):
            break
        if low == "reset":
            agent.reset_session()
            print("会话已重置。")
            continue
        if low == "skills":
            sm = getattr(agent, "skill_manager", None)
            if sm and sm.enabled:
                print(f"已加载 {len(sm.skill_names)} 个技能: {sm.skill_names}")
            else:
                print("技能系统未启用")
            continue
        if low == "memory":
            mm = getattr(agent, "memory_manager", None)
            if mm and mm.enabled:
                print(f"短期记忆: {mm.stm.facts or '（无）'}")
                print(f"长期档案: {[f'[{f.category}]{f.content}' for f in mm.ltm.facts] or '（无）'}")
            else:
                print("记忆功能未启用")
            continue
        if low == "summary":
            print(f"历史摘要: {agent.summary or '（无，未触发压缩）'}")
            continue

        try:
            resp = agent.chat(user_input)
            print(f"\n⚖️ 小律: {resp.reply}")
            print(f"   [意图: {INTENT_LABELS.get(resp.intent, resp.intent.value)}"
                  f" | 置信度: {resp.confidence:.0%}"
                  f" | 紧急度: {URGENCY_LABELS.get(resp.urgency.value, resp.urgency.value)}"
                  f" | 转人工: {'是' if resp.requires_human else '否'}]")
            if resp.follow_up_question:
                print(f"   [追问: {resp.follow_up_question}]")
        except Exception as e:  # noqa: BLE001 —— 单轮失败不清空会话
            print(f"\n⚠️  出错了: {e}")

    agent.save()
    agent.close()
    print(agent.tracer.summary())
    print("再见，祝维权顺利！")


if __name__ == "__main__":
    main()
