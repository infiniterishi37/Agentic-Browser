from typing import TypedDict, Annotated, List, Dict, Any
from langchain_core.messages import BaseMessage
import operator

class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], operator.add]
    plan: List[Dict[str, Any]]
    browser_output: str
    critique: str
    task_complete: bool
    # Orchestration fields
    session_id: str
    token_id: str
    orchestration_place: str
    epistemic_confidence: float
    replanning_count: int
    # LLM override fields (set from chat panel selection)
    llm_provider: str
    llm_model: str
