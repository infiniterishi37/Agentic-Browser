"""
Multi-Agent Agentic AI Web Orchestration Framework
===================================================

Comparative implementation of three multi-agent orchestration architectures:
  - Architecture A: Pydantic AI + AutoGen
  - Architecture B: LangGraph + AutoGen  
  - Architecture C: LangChain + CrewAI

Each architecture coordinates three specialized agents:
  1. Planner Agent   - GPT-4 hierarchical task decomposition
  2. Browser Agent   - Playwright-based hybrid automation
  3. Critic Agent    - LLM-based verification & anomaly detection

Reference: "Multi-Agent Agentic AI Web Orchestration: A Novel Framework
for Autonomous Browser Automation" (Singh et al., 2026)
"""

__version__ = "1.0.0"
__all__ = [
    "pydantic_autogen",
    "langgraph_autogen",
    "crewai_langchain",
    "tb_cspn",
    "state_manager",
    "models",
]
