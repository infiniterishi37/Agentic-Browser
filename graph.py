from typing import Literal
from langgraph.graph import StateGraph
from agent.state import AgentState
from agent.planner import planner_node
from agent.browser import browser_node
from agent.critique import critique_node
from dotenv import load_dotenv

load_dotenv()

# --- Workflow Definition ---

MAX_REPLANNING_CYCLES = 5

def should_continue(state: AgentState) -> Literal["planner", "__end__"]:
    if state.get("task_complete"):
        print("\n✅ Task Completed!")
        return "__end__"
    
    # Check replanning limit from orchestration
    replanning = state.get("replanning_count", 0)
    if replanning >= MAX_REPLANNING_CYCLES:
        print(f"\n⚠️ Max replanning cycles ({MAX_REPLANNING_CYCLES}) reached. Stopping.")
        return "__end__"

    # Check orchestration confidence
    confidence = state.get("epistemic_confidence", 1.0)
    place = state.get("orchestration_place", "")
    if place == "awaiting_human":
        print("\n🧑 Human intervention requested by orchestration. Stopping for feedback.")
        return "__end__"
    
    return "planner"

workflow = StateGraph(AgentState)

workflow.add_node("planner", planner_node)
workflow.add_node("browser", browser_node)
workflow.add_node("critique", critique_node)

# Flow: Planner -> Browser -> Critique -> (Planner or End)
workflow.set_entry_point("planner")
workflow.add_edge("planner", "browser")
workflow.add_edge("browser", "critique")
workflow.add_conditional_edges("critique", should_continue)

app = workflow.compile()
