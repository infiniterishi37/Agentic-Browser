import logging
import os
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from agent.state import AgentState
from agent.tools import TOOL_CONFIG
from agent.llm_provider import get_llm
from orchestration.engine import orchestration_engine
from tools.chat_server import chat_server

logger = logging.getLogger(__name__)


def _compact_text(text: str, max_chars: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    head = int(max_chars * 0.7)
    tail = max_chars - head
    return text[:head] + "\n...[truncated]...\n" + text[-tail:]


async def planner_node(state: AgentState):
    print("\n🧠 [Planner]: Thinking...")
    messages = state['messages']
    critique = state.get('critique', "No critique yet (first step).")
    browser_output = state.get('browser_output', "")
    session_id = state.get('session_id', '')
    token_id = state.get('token_id', '')

    # Build LLM dynamically from state (chat-panel selection or .env defaults)
    llm = get_llm(
        provider=state.get('llm_provider') or None,
        model=state.get('llm_model') or None,
    )
    
    # Generate tool descriptions dynamically
    tool_descriptions = "\n".join([f"- {name}: {config['desc']}" for name, config in TOOL_CONFIG.items()])
    
    prompt = ChatPromptTemplate.from_messages([
        ("system", """You are a Browser Planning Agent.
Your goal is to create a precise, step-by-step plan for a Browser Agent to execute.
The Browser Agent has the following tools:
{tool_descriptions}

INPUT Context:
- User Request: {user_request}
- Previous Browser Output: {browser_output}
- Critique/Feedback: {critique}

SHOPPING RULE (HIGHEST PRIORITY):
If the user request is about shopping, buying, purchasing, finding items to buy, or comparing
prices across stores — you MUST use ONLY the `shop_all_platforms` tool as a single step.
Do NOT add any other steps. Do NOT use google_search or navigate separately.
Example:
  User: "buy milk and eggs"
  Plan: [{{"tool": "shop_all_platforms", "args": {{"query": "milk and eggs"}}}}]

INSTRUCTIONS (for non-shopping tasks):
1. Analyze the context and the user's goal.
2. If the Feedback says "Success", output an empty plan [].
3. Otherwise, create a sequence of actions (max 5 steps) to move towards the goal.
4. If you need to see the page, always include `read_page` or `list_interactive_elements` in your plan.
5. If searching, verify results with `read_page`.
6. CRITICAL: If the user says "Open X", simply searching for X is NOT enough. You MUST click the link to actually open the site.
7. CRITICAL: If you performed a search, your NEXT step MUST be to `read_page` or `list_interactive_elements` to find the link, and then `click_element`.

IMPORTANT:
- Return ONLY valid JSON.
- Do not add any markdown formatting (like ```json).
- Do not add any text before or after the JSON.

OUTPUT FORMAT:
Return a JSON array of objects, where each object has "tool" (string) and "args" (dictionary).
Example:
[
    {{"tool": "google_search", "args": {{"query": "python tutorial"}}}},
    {{"tool": "read_page", "args": {{}}}}
]
"""),
        ("human", "Current State: {critique}. Create the next plan.")
    ])

    
    # Extract user request from the first message
    user_request = messages[0].content if messages else "Unknown task"
    req_max = int(os.getenv("LLM_MAX_USER_REQUEST_CHARS", "500"))
    out_max = int(os.getenv("LLM_MAX_BROWSER_OUTPUT_CHARS", "1800"))
    crit_max = int(os.getenv("LLM_MAX_CRITIQUE_CHARS", "700"))
    user_request = _compact_text(user_request, req_max)
    browser_output = _compact_text(browser_output, out_max)
    critique = _compact_text(critique, crit_max)
    
    chain = prompt | llm | JsonOutputParser()
    
    provider = state.get("llm_provider") or "default"
    model = state.get("llm_model") or "default"

    try:
        await chat_server.send_to_browser(
            f"🧠 Planner calling {provider}/{model}...", "status"
        )
        logger.info("Planner LLM call start provider=%s model=%s", provider, model)
        plan = await chain.ainvoke({
            "tool_descriptions": tool_descriptions,
            "user_request": user_request,
            "browser_output": browser_output,
            "critique": critique
        })
        
        # Robustness: Ensure plan is a list
        if isinstance(plan, dict):
            plan = [plan]
            
        print(f"🧠 [Planner]: Plan generated with {len(plan)} steps.")
        logger.info(
            "Planner LLM call success provider=%s model=%s steps=%s",
            provider,
            model,
            len(plan),
        )
        print(plan)

        # ── Orchestration: record the structured plan ──
        if session_id and token_id:
            try:
                await orchestration_engine.on_plan_generated(
                    session_id, token_id, plan, user_request
                )
            except Exception as oe:
                print(f"🧠 [Planner/Orchestration Warning]: {oe}")

        replanning_count = state.get('replanning_count', 0)
        return {
            "plan": plan,
            "critique": "",
            "replanning_count": replanning_count + 1,
        }
    except Exception as e:
        print(f"🧠 [Planner Error]: {e}")
        logger.exception(
            "Planner LLM call failed provider=%s model=%s error=%s",
            provider,
            model,
            e,
        )
        await chat_server.send_to_browser(
            f"❌ Planner error: {type(e).__name__}: {e}", "system"
        )
        raise
