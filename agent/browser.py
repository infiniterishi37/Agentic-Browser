from agent.state import AgentState
from agent.tools import TOOL_CONFIG
from orchestration.engine import orchestration_engine
from tools.browser import browser_manager
from tools.chat_server import chat_server

async def browser_node(state: AgentState):
    print("\n🌐 [Browser]: Executing plan...")
    await chat_server.send_to_browser("🌐 Executing browser actions...", "status")
    plan = state.get('plan', [])
    session_id = state.get('session_id', '')
    token_id = state.get('token_id', '')
    execution_log = ""
    
    if not plan:
        return {"browser_output": "No plan to execute."}

    for step in plan:
        tool_name = step.get('tool')
        tool_args = dict(step.get('args', {}) or {})
        
        if tool_name in TOOL_CONFIG:
            print(f"   > Action: {tool_name} {tool_args}")
            try:
                # Ensure shopping flow uses chat-selected model/provider.
                if tool_name == "shop_all_platforms":
                    tool_args.setdefault("llm_provider", state.get("llm_provider", ""))
                    tool_args.setdefault("llm_model", state.get("llm_model", ""))

                # Call the tool function directly from browser_manager via mapping
                func = TOOL_CONFIG[tool_name]['func']
                if tool_args:
                    result = await func(**tool_args)
                else:
                    result = await func()
                
                execution_log += f"Action: {tool_name}\nResult: {result}\n\n"
            except Exception as e:
                execution_log += f"Action: {tool_name}\nError: {str(e)}\n\n"
        else:
            execution_log += f"Action: {tool_name}\nError: Tool not found.\n\n"

    # ── Orchestration: record action results ──
    if session_id and token_id:
        try:
            current_url = ""
            page_title = ""
            if browser_manager.page:
                try:
                    current_url = browser_manager.page.url
                    page_title = await browser_manager.page.title()
                except Exception:
                    pass

            await orchestration_engine.on_action_executed(
                session_id, token_id, execution_log,
                current_url=current_url,
                page_title=page_title,
            )
        except Exception as oe:
            print(f"🌐 [Browser/Orchestration Warning]: {oe}")

    return {"browser_output": execution_log}
