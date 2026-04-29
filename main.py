import os
import sys
import asyncio
import logging
import socket
import uuid
import time
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage
from graph import app
from tools.browser import browser_manager
from tools.chat_server import chat_server
from tools.flights.assistant import handle_flight_chat_message
from orchestration.engine import orchestration_engine

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
# Keep orchestration logs at INFO, but quiet down noisy libraries
logging.getLogger("orchestration").setLevel(logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("langchain").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)

IDLE_STOP_SECONDS = int(os.getenv("IDLE_STOP_SECONDS", "600"))


def _resolve_provider_and_model(
    llm_provider: str = "", llm_model: str = ""
) -> tuple[str, str]:
    """Resolve effective provider/model using chat overrides then env defaults."""
    provider = (llm_provider or os.getenv("LLM_PROVIDER", "google")).lower().strip()
    if provider == "google":
        model = llm_model or os.getenv("MODEL", "gemini-2.5-flash-lite")
    elif provider == "groq":
        model = llm_model or os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    else:
        model = llm_model or ""
    return provider, model


def _validate_provider_config(provider: str) -> None:
    """Fail fast on missing provider credentials."""
    if provider == "google":
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key or api_key == "your_gemini_api_key_here":
            raise EnvironmentError("GOOGLE_API_KEY is missing or invalid for Google provider.")
        return
    if provider == "groq":
        api_key = os.getenv("GROQ_API_KEY") or os.getenv("GROK_API_KEY")
        if not api_key:
            raise EnvironmentError("GROQ_API_KEY is missing for Groq provider.")
        # Preflight network check so failures are reported immediately.
        try:
            socket.getaddrinfo("api.groq.com", 443, type=socket.SOCK_STREAM)
        except OSError as e:
            raise ConnectionError(
                f"Cannot reach Groq endpoint (api.groq.com): {e}"
            ) from e
        return
    raise ValueError("Unknown provider. Use 'google' or 'groq'.")


async def _run_graph(initial_state: dict) -> None:
    """Run the LangGraph workflow to completion."""
    async for _event in app.astream(initial_state):
        pass


async def run_task(
    user_input: str,
    llm_provider: str = "",
    llm_model: str = "",
    loop_limit: int = 3,
) -> None:
    """Execute a single user task through the orchestrated agent pipeline."""
    run_id = uuid.uuid4().hex[:8]
    print(f"\n{'─' * 50}")
    print(f"📋 Task[{run_id}]: {user_input}")
    print(f"{'─' * 50}")

    provider, model = _resolve_provider_and_model(llm_provider, llm_model)
    if loop_limit < 1:
        loop_limit = 1
    await chat_server.send_to_browser(
        (
            f"Starting now: I understood your request and I'm spinning up the workflow "
            f"with {provider}/{model} (task id: {run_id}, loop limit: {loop_limit})."
        ),
        "status",
    )
    await chat_server.update_agent_state(
        running=True,
        run_id=run_id,
        provider=provider,
        model=model,
        loop_limit=loop_limit,
        last_error="",
    )

    session_id = ""
    token_id = ""
    try:
        await chat_server.send_to_browser(
            "Quick check: validating provider configuration and connectivity.",
            "status",
        )
        _validate_provider_config(provider)

        # Start orchestration session
        await chat_server.send_to_browser(
            "Great, configuration is valid. I'm creating an execution session.",
            "status",
        )
        ctx = await orchestration_engine.start_task(user_input)
        session_id = ctx["session_id"]
        token_id = ctx["token_id"]

        print(f"🔑 Run[{run_id}] Session: {session_id} | Token: {token_id}")

        # Initial state with orchestration context
        initial_state = {
            "messages": [HumanMessage(content=user_input)],
            "plan": [],
            "browser_output": "",
            "critique": "",
            "task_complete": False,
            "session_id": session_id,
            "token_id": token_id,
            "orchestration_place": "planning",
            "epistemic_confidence": 1.0,
            "replanning_count": 0,
            "llm_provider": provider,
            "llm_model": model,
            "loop_limit": loop_limit,
        }

        await chat_server.send_to_browser(
            "Plan is ready. I am now running planner, browser actions, and critique checks.",
            "status",
        )
        await _run_graph(initial_state)

        # Finalize orchestration
        await chat_server.send_to_browser(
            "Finalizing outputs and wrapping up this run.",
            "status",
        )
        await orchestration_engine.finalize(session_id, token_id)

        # Send completion to browser chat
        await chat_server.send_to_browser(
            f"✅ Task {run_id} completed: {user_input[:50]}...", "system"
        )
        await chat_server.update_agent_state(
            running=False,
            run_id=run_id,
            provider=provider,
            model=model,
            loop_limit=loop_limit,
            last_error="",
        )
    except Exception as e:
        err_msg = f"❌ Task {run_id} failed: {type(e).__name__}: {e}"
        print(err_msg)
        import traceback
        traceback.print_exc()
        await chat_server.send_to_browser(err_msg, "system")
        await chat_server.update_agent_state(
            running=False,
            run_id=run_id,
            provider=provider,
            model=model,
            loop_limit=loop_limit,
            last_error=f"{type(e).__name__}: {e}",
        )



async def check_chat_messages() -> dict | None:
    """Non-blocking check for messages from the browser chat widget."""
    return await chat_server.get_message(timeout=0.1)


def _safe_loop_limit(value, default: int = 3) -> int:
    try:
        parsed = int(value)
    except Exception:
        return default
    return parsed if parsed >= 1 else default


async def run_agent():
    print("╔══════════════════════════════════════════════════╗")
    print("║       Agentic Browser — Orchestrated Agent       ║")
    print("║    with TB-CSPN + Chat Interface (Gemini 2.5)    ║")
    print("╚══════════════════════════════════════════════════╝")

    provider = os.getenv("LLM_PROVIDER", "google").lower().strip()
    print(f"    🤖 LLM Provider: {provider.upper()}")

    if provider == "google":
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key or api_key == "your_gemini_api_key_here":
            print("Error: Valid GOOGLE_API_KEY not found in .env file.")
            print("Please set GOOGLE_API_KEY in your .env file.")
            return
    elif provider == "groq":
        api_key = os.getenv("GROQ_API_KEY") or os.getenv("GROK_API_KEY")
        if not api_key:
            print("Error: GROQ_API_KEY not found in .env file.")
            print("Please set GROQ_API_KEY in your .env file.")
            return
    else:
        print(f"Error: Unknown LLM_PROVIDER='{provider}'. Use 'google' or 'groq'.")
        return

    # Initialize orchestration engine
    print("\n⚙️  Initializing orchestration engine...")
    await orchestration_engine.initialize()

    # Start browser
    print("🌐 Starting browser...")
    await browser_manager.start()

    # Start chat WebSocket server
    print("💬 Starting chat server...")
    await chat_server.start()

    pending_cli_input = None
    last_activity_ts = time.monotonic()
    try:
        while True:
            try:
                # Check for messages from browser chat widget
                chat_msg = await check_chat_messages()
                if chat_msg:
                    last_activity_ts = time.monotonic()
                    print(f"\n💬 [Chat Message]: {chat_msg}")
                    handled = await handle_flight_chat_message(chat_msg.get("content", ""))
                    if handled:
                        continue
                    await run_task(
                        chat_msg.get("content", ""),
                        chat_msg.get("provider", ""),
                        chat_msg.get("model", ""),
                        _safe_loop_limit(chat_msg.get("loop_limit", 3)),
                    )
                    continue

                # CLI input: keep a single pending input future to avoid
                # leaking executor threads with repeated input() calls.
                if pending_cli_input is None:
                    pending_cli_input = asyncio.get_event_loop().run_in_executor(
                        None, lambda: input("\nRequest (or 'q' to quit): ")
                    )

                try:
                    user_input = await asyncio.wait_for(
                        asyncio.shield(pending_cli_input),
                        timeout=0.25,
                    )
                    pending_cli_input = None
                except asyncio.TimeoutError:
                    if (time.monotonic() - last_activity_ts) >= IDLE_STOP_SECONDS:
                        print(
                            f"\n🛑 No communication for {IDLE_STOP_SECONDS}s. Stopping."
                        )
                        await chat_server.send_to_browser(
                            f"🛑 Idle for {IDLE_STOP_SECONDS}s. Stopping agent.",
                            "system",
                        )
                        break
                    continue

                if user_input.lower() in ["q", "quit", "exit"]:
                    break

                if user_input.strip():
                    last_activity_ts = time.monotonic()
                    await run_task(user_input)

            except KeyboardInterrupt:
                break
            except Exception as e:
                err_msg = f"❌ Runtime error: {type(e).__name__}: {e}"
                print(err_msg)
                await chat_server.send_to_browser(err_msg, "system")
                import traceback
                traceback.print_exc()
    finally:
        print("\n🛑 Shutting down...")
        await chat_server.stop()
        await browser_manager.stop()
        await orchestration_engine.shutdown()
        print("👋 Goodbye!")


def main():
    asyncio.run(run_agent())


if __name__ == "__main__":
    main()
