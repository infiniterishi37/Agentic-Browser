"""
Orchestration Engine
====================

Bridge between TB-CSPN / StateManager and the LangGraph workflow.
Provides a clean interface for the agent nodes to record structured
plans, actions, and verifications while the Petri Net manages workflow
state and the StateManager handles persistence.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from orchestration.models import (
    ActionResult,
    ActionType,
    AgentMessage,
    BeliefState,
    ExecutionTrace,
    PlanStep,
    TaskPlan,
    TaskStatus,
    VerificationResult,
    VerificationStatus,
)
from orchestration.state_manager import StateManager
from orchestration.tb_cspn import TBCSPN, PlaceType

logger = logging.getLogger(__name__)


class OrchestrationEngine:
    """
    Central coordinator that integrates TB-CSPN token management,
    topic-channel messaging, and state persistence into a single
    interface consumed by the LangGraph agent nodes.

    Usage:
        engine = OrchestrationEngine()
        await engine.initialize()

        ctx = await engine.start_task("Search for Python tutorials")
        # ... planner/browser/critique nodes call engine methods ...
        await engine.finalize(ctx["session_id"])
    """

    def __init__(
        self,
        human_confidence_threshold: float = 0.3,
        max_replanning_cycles: int = 5,
    ):
        self.cspn = TBCSPN(
            human_confidence_threshold=human_confidence_threshold,
            max_replanning_cycles=max_replanning_cycles,
        )
        self.state_manager = StateManager()
        self._initialized = False

    # ─── Lifecycle ────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """Set up storage backends and default topic channels."""
        await self.state_manager.initialize()

        # Create standard communication channels
        self.cspn.create_topic("planning", "Coordinates task decomposition")
        self.cspn.create_topic("execution", "Manages browser action execution")
        self.cspn.create_topic("verification", "Handles output verification")
        self.cspn.create_topic("human", "Human-in-the-loop intervention channel")

        # Register human callback
        self.cspn.set_human_callback(self._on_human_intervention)

        self._initialized = True
        logger.info("OrchestrationEngine initialized")

    async def shutdown(self) -> None:
        """Clean shutdown of all backends."""
        await self.state_manager.shutdown()
        logger.info("OrchestrationEngine shut down")

    # ─── Task Lifecycle ──────────────────────────────────────────────────

    async def start_task(self, user_request: str) -> Dict[str, Any]:
        """
        Begin a new task execution.
        Creates a session, belief state, and Petri Net token.
        Returns a context dict with session_id, token_id, etc.
        """
        session_id = f"session_{uuid.uuid4().hex[:8]}"
        token_id = f"token_{uuid.uuid4().hex[:8]}"

        # Create belief state
        belief_state = BeliefState(
            task_context=user_request,
            epistemic_confidence=1.0,
            active_topics=["planning", "execution", "verification"],
        )

        # Create session in state manager
        await self.state_manager.create_session(session_id, belief_state)

        # Create token in Petri Net
        token = self.cspn.create_token(token_id, belief_state, PlaceType.IDLE)

        # Transition: IDLE → PLANNING
        await self.cspn.try_fire(token)

        # Publish task start message
        await self.cspn.send_message(
            "planning",
            AgentMessage(
                sender="manager",
                receiver="planner",
                content=f"New task: {user_request}",
                message_type="action",
            ),
        )

        logger.info(
            f"Task started — session={session_id}, token={token_id}, "
            f"place={token.place.value}"
        )

        return {
            "session_id": session_id,
            "token_id": token_id,
            "belief_state": belief_state,
        }

    # ─── Agent Node Hooks ────────────────────────────────────────────────

    async def on_plan_generated(
        self,
        session_id: str,
        token_id: str,
        raw_plan: List[Dict[str, Any]],
        user_request: str,
    ) -> None:
        """Called by the planner node after generating a plan."""
        token = self.cspn.get_token(token_id)
        if not token:
            logger.error(f"Token {token_id} not found")
            return

        # Convert raw plan dicts to structured PlanSteps
        steps = []
        for step_dict in raw_plan:
            tool = step_dict.get("tool", "custom")
            args = step_dict.get("args", {})

            # Map tool name to ActionType
            action_map = {
                "navigate": ActionType.NAVIGATE,
                "google_search": ActionType.GOOGLE_SEARCH,
                "click_element": ActionType.CLICK,
                "type_text": ActionType.TYPE_TEXT,
                "read_page": ActionType.READ_PAGE,
                "list_interactive_elements": ActionType.LIST_ELEMENTS,
                "scroll": ActionType.SCROLL,
                "amazon_shop": ActionType.CUSTOM,
            }
            action_type = action_map.get(tool, ActionType.CUSTOM)

            steps.append(
                PlanStep(
                    action_type=action_type,
                    target=args.get("url") or args.get("selector") or args.get("query"),
                    parameters=args,
                    expected_outcome=f"Execute {tool} with args {args}",
                )
            )

        # Build structured plan
        task_plan = TaskPlan(
            user_request=user_request,
            steps=steps,
        )

        # Record in state manager
        await self.state_manager.record_plan(session_id, task_plan)

        # Update belief state
        belief = token.belief_state
        belief.plan_history.append(task_plan)

        # Transition: PLANNING → EXECUTING
        await self.cspn.transition(token, PlaceType.EXECUTING)

        # Publish to topic
        await self.cspn.send_message(
            "planning",
            AgentMessage(
                sender="planner",
                receiver="browser",
                content=f"Plan generated with {len(steps)} steps",
                message_type="action",
                plan_step=steps[0] if steps else None,
            ),
        )

        logger.info(
            f"[{session_id}] Plan recorded: {len(steps)} steps, "
            f"token → {token.place.value}"
        )

    async def on_action_executed(
        self,
        session_id: str,
        token_id: str,
        execution_log: str,
        current_url: str = "",
        page_title: str = "",
    ) -> None:
        """Called by the browser node after executing actions."""
        token = self.cspn.get_token(token_id)
        if not token:
            logger.error(f"Token {token_id} not found")
            return

        # Parse execution log into ActionResults
        action_results = []
        for block in execution_log.strip().split("\n\n"):
            lines = block.strip().split("\n")
            if not lines:
                continue

            action_line = lines[0] if lines else ""
            result_line = lines[1] if len(lines) > 1 else ""

            tool_name = action_line.replace("Action: ", "").strip()
            is_error = result_line.startswith("Error:")
            output = result_line.replace("Result: ", "").replace("Error: ", "").strip()

            action_result = ActionResult(
                step_id=str(uuid.uuid4())[:8],
                action_type=ActionType.CUSTOM,
                success=not is_error,
                output=output[:500],
                error=output if is_error else None,
            )
            action_results.append(action_result)

        # Record each action
        for ar in action_results:
            await self.state_manager.record_action(session_id, ar)

        # Update belief state
        belief = token.belief_state
        belief.action_history.extend(action_results)
        belief.current_url = current_url or None
        belief.current_page_title = page_title or None

        # Adjust confidence based on success rate
        if action_results:
            success_rate = sum(1 for a in action_results if a.success) / len(action_results)
            belief.epistemic_confidence = round(
                belief.epistemic_confidence * 0.7 + success_rate * 0.3, 3
            )

        # Transition: EXECUTING → VERIFYING
        await self.cspn.transition(token, PlaceType.VERIFYING)

        await self.state_manager.update_belief_state(session_id, belief)

        # Publish to topic
        await self.cspn.send_message(
            "execution",
            AgentMessage(
                sender="browser",
                receiver="critic",
                content=f"Executed {len(action_results)} actions, "
                        f"success_rate={sum(1 for a in action_results if a.success)}/{len(action_results)}",
                message_type="action",
                action_result=action_results[-1] if action_results else None,
            ),
        )

        logger.info(
            f"[{session_id}] Actions recorded: {len(action_results)} results, "
            f"confidence={belief.epistemic_confidence:.2f}"
        )

    async def on_verification_complete(
        self,
        session_id: str,
        token_id: str,
        feedback: str,
        task_complete: bool,
    ) -> Dict[str, Any]:
        """
        Called by the critique node after generating feedback.
        Returns updated orchestration state for the graph.
        """
        token = self.cspn.get_token(token_id)
        if not token:
            logger.error(f"Token {token_id} not found")
            return {"orchestration_place": "error", "epistemic_confidence": 0.0}

        belief = token.belief_state

        # Determine verification status
        if task_complete:
            status = VerificationStatus.SUCCESS
            confidence = 0.95
        elif "error" in feedback.lower() or "fail" in feedback.lower():
            status = VerificationStatus.FAILURE
            confidence = max(0.1, belief.epistemic_confidence - 0.3)
            belief.error_count += 1
        else:
            status = VerificationStatus.PARTIAL
            confidence = max(0.2, belief.epistemic_confidence - 0.1)

        verification = VerificationResult(
            step_id=str(uuid.uuid4())[:8],
            status=status,
            confidence=confidence,
            issues=[feedback] if not task_complete else [],
            recommendations=[],
            requires_replanning=not task_complete,
        )

        await self.state_manager.record_verification(session_id, verification)

        # Update belief state confidence
        belief.epistemic_confidence = confidence
        await self.state_manager.update_belief_state(session_id, belief)

        # Publish to topic
        await self.cspn.send_message(
            "verification",
            AgentMessage(
                sender="critic",
                receiver="manager",
                content=f"Verification: {status.value} (confidence={confidence:.2f})",
                message_type="verification",
                verification_result=verification,
            ),
        )

        # Determine next state via Petri Net
        if task_complete:
            await self.cspn.transition(token, PlaceType.COMPLETED)
        else:
            # Try to fire — Petri Net will decide between replanning, human, etc.
            fired = await self.cspn.try_fire(token)
            if fired is None:
                # Fallback: go to replanning
                await self.cspn.transition(token, PlaceType.REPLANNING)
                await self.cspn.transition(token, PlaceType.PLANNING)

        logger.info(
            f"[{session_id}] Verification: {status.value}, "
            f"confidence={confidence:.2f}, place={token.place.value}"
        )

        return {
            "orchestration_place": token.place.value,
            "epistemic_confidence": belief.epistemic_confidence,
        }

    # ─── Finalization ────────────────────────────────────────────────────

    async def finalize(self, session_id: str, token_id: str) -> None:
        """Build execution trace, persist, and print diagnostics."""
        token = self.cspn.get_token(token_id)
        if not token:
            return

        belief = token.belief_state
        plan = belief.plan_history[-1] if belief.plan_history else TaskPlan(
            user_request="unknown", steps=[]
        )

        trace = ExecutionTrace(
            task_plan=plan,
            action_results=belief.action_history,
            total_steps=len(belief.action_history),
            replanning_cycles=len(belief.plan_history) - 1 if belief.plan_history else 0,
            final_status=(
                TaskStatus.COMPLETED
                if token.place == PlaceType.COMPLETED
                else TaskStatus.FAILED
            ),
            completed_at=datetime.utcnow(),
        )

        await self.state_manager.finalize_session(session_id, trace)

        # Print diagnostics
        net_state = self.cspn.get_network_state()
        print("\n" + "=" * 60)
        print("📊 ORCHESTRATION DIAGNOSTICS")
        print("=" * 60)
        print(f"  Session:    {session_id}")
        print(f"  Token:      {token_id} → {token.place.value}")
        print(f"  Confidence: {belief.epistemic_confidence:.2f}")
        print(f"  Actions:    {len(belief.action_history)}")
        print(f"  Plans:      {len(belief.plan_history)}")
        print(f"  Errors:     {belief.error_count}")
        print(f"  Topics:     {list(net_state['topics'].keys())}")
        print(f"  Transitions fired:")
        for t_name, count in net_state["transition_fire_counts"].items():
            if count > 0:
                print(f"    {t_name}: {count}")
        print("=" * 60 + "\n")

    # ─── Human-in-the-Loop ──────────────────────────────────────────────

    async def _on_human_intervention(self, token) -> None:
        """Callback triggered when confidence drops below threshold."""
        print("\n" + "⚠️" * 20)
        print("🧑 HUMAN INTERVENTION REQUESTED")
        print(f"   Token: {token.token_id}")
        print(f"   Confidence: {token.belief_state.epistemic_confidence:.2f}")
        print(f"   Errors: {token.belief_state.error_count}")
        print("   The agent is uncertain. Please provide guidance.")
        print("⚠️" * 20 + "\n")

    def provide_human_feedback(self, token_id: str, feedback: str) -> None:
        """Inject human feedback into the token's belief state."""
        self.cspn.provide_human_feedback(token_id, feedback)


# ─── Global singleton ────────────────────────────────────────────────────────

orchestration_engine = OrchestrationEngine()
