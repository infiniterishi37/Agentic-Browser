import logging
import os
from langchain_core.prompts import ChatPromptTemplate
from agent.state import AgentState
from agent.llm_provider import get_llm
from tools.browser import browser_manager
from tools.chat_server import chat_server
from orchestration.engine import orchestration_engine

logger = logging.getLogger(__name__)


def _compact_text(text: str, max_chars: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    head = int(max_chars * 0.7)
    tail = max_chars - head
    return text[:head] + "\n...[truncated]...\n" + text[-tail:]


async def critique_node(state: AgentState):
    print("\n🧐 [Critique]: Analyzing...")
    messages = state['messages']
    browser_output = state.get('browser_output', "")
    user_request = messages[0].content
    session_id = state.get('session_id', '')
    token_id = state.get('token_id', '')

    # Build LLM dynamically from state (chat-panel selection or .env defaults)
    llm = get_llm(
        provider=state.get('llm_provider') or None,
        model=state.get('llm_model') or None,
    )
    
    # Get current page state (live)
    current_content = await browser_manager.get_content()
    # Strict input budgeting for smaller-context models.
    req_max = int(os.getenv("LLM_MAX_USER_REQUEST_CHARS", "500"))
    out_max = int(os.getenv("LLM_MAX_BROWSER_OUTPUT_CHARS", "1800"))
    page_max = int(os.getenv("LLM_MAX_PAGE_CONTENT_CHARS", "2000"))
    user_request = _compact_text(user_request, req_max)
    browser_output = _compact_text(browser_output, out_max)
    current_content = _compact_text(current_content, page_max)

    prompt = ChatPromptTemplate.from_messages([
        ("system", """You are a Critique Agent.
Your job is to evaluate if the Browser Agent has successfully completed the User's Request.

User Request: {user_request}

Browser execution logs:
{browser_output}

Current Page Content:
{current_content}

INSTRUCTIONS:
1. Did the actions succeed?
2. Is the goal accomplished? (e.g. is the information found? is the item in cart?)
3. If YES, return exactly: "Success"
4. If NO, analyze what went wrong or what is missing. Provide specific feedback for the Planner to create the next plan.

Output ONLY the feedback string.
"""),
        ("human", "Evaluate the progress.")
    ])
    
    # Create the chain here
    chain = prompt | llm
    
    provider = state.get("llm_provider") or "default"
    model = state.get("llm_model") or "default"

    try:
        await chat_server.send_to_browser(
            f"🧐 Critic calling {provider}/{model}...", "status"
        )
        logger.info("Critique LLM call start provider=%s model=%s", provider, model)
        response = await chain.ainvoke({
            "user_request": user_request,
            "browser_output": browser_output,
            "current_content": current_content
        })
        logger.info("Critique LLM call success provider=%s model=%s", provider, model)
    except Exception as e:
        logger.exception(
            "Critique LLM call failed provider=%s model=%s error=%s",
            provider,
            model,
            e,
        )
        await chat_server.send_to_browser(
            f"❌ Critique error: {type(e).__name__}: {e}", "system"
        )
        raise
    
    feedback = response.content.strip()
    print(f"🧐 [Critique]: {feedback}")
    
    task_complete = "Success" in feedback

    # ── Orchestration: record verification result ──
    orch_state = {}
    if session_id and token_id:
        try:
            orch_state = await orchestration_engine.on_verification_complete(
                session_id, token_id, feedback, task_complete
            )
        except Exception as oe:
            print(f"🧐 [Critique/Orchestration Warning]: {oe}")

    return {
        "critique": feedback,
        "task_complete": task_complete,
        "orchestration_place": orch_state.get("orchestration_place", ""),
        "epistemic_confidence": orch_state.get("epistemic_confidence", 1.0),
    }
