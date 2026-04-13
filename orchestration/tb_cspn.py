"""
Topic-Based Communication-Space Petri Net (TB-CSPN)
====================================================

Implementation of dynamic topic-driven group formation and semantic
self-organization for multi-agent coordination.

Reference: Borghoff, Bottoni & Pareschi (2025) — "An organizational theory
for multi-agent interactions integrating human agents, LLMs, and specialized AI"

The TB-CSPN manages:
  1. Dynamic topic channels for agent communication
  2. Human-in-the-loop intervention triggers
  3. Belief state synchronization across agents
  4. Token/place management for workflow control
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set

from orchestration.models import AgentMessage, BeliefState

logger = logging.getLogger(__name__)


# ─── Petri Net Primitives ────────────────────────────────────────────────────

class PlaceType(str, Enum):
    """Types of places (states) in the Communication-Space Petri Net."""
    IDLE = "idle"
    PLANNING = "planning"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    AWAITING_HUMAN = "awaiting_human"
    REPLANNING = "replanning"
    COMPLETED = "completed"
    ERROR = "error"


class Token:
    """
    Token in the Petri Net representing a unit of work or control flow.
    Carries belief state and execution context through the network.
    """

    def __init__(
        self,
        token_id: str,
        place: PlaceType,
        belief_state: BeliefState,
        metadata: Optional[Dict[str, Any]] = None
    ):
        self.token_id = token_id
        self.place = place
        self.belief_state = belief_state
        self.metadata = metadata or {}
        self.created_at = datetime.utcnow()
        self.transition_history: List[Dict[str, Any]] = []

    def move_to(self, new_place: PlaceType, reason: str = "") -> None:
        """Transition token to a new place, recording the transition."""
        self.transition_history.append({
            "from": self.place.value,
            "to": new_place.value,
            "reason": reason,
            "timestamp": datetime.utcnow().isoformat()
        })
        self.place = new_place
        logger.debug(f"Token {self.token_id}: {self.place.value} → {new_place.value} ({reason})")


class Transition:
    """
    Transition in the Petri Net — fires when guard conditions are met,
    consuming input tokens and producing output tokens.
    """

    def __init__(
        self,
        name: str,
        input_places: List[PlaceType],
        output_places: List[PlaceType],
        guard: Optional[Callable[[Token], bool]] = None,
        action: Optional[Callable[[Token], Any]] = None,
        priority: int = 0
    ):
        self.name = name
        self.input_places = input_places
        self.output_places = output_places
        self.guard = guard or (lambda _: True)
        self.action = action
        self.priority = priority
        self.fire_count = 0

    def is_enabled(self, token: Token) -> bool:
        """Check if transition can fire for the given token."""
        return token.place in self.input_places and self.guard(token)

    async def fire(self, token: Token) -> PlaceType:
        """Execute transition, returning the destination place."""
        if self.action:
            await self.action(token) if asyncio.iscoroutinefunction(self.action) else self.action(token)
        self.fire_count += 1
        # Select first output place (deterministic) or conditional
        dest = self.output_places[0]
        token.move_to(dest, reason=f"Transition: {self.name}")
        return dest


# ─── Topic Channel ───────────────────────────────────────────────────────────

class TopicChannel:
    """
    Communication channel for a specific topic within the TB-CSPN.
    Agents subscribe to topics and exchange structured messages.
    Implements semantic self-organization through dynamic topic creation.
    """

    def __init__(self, topic_name: str, description: str = ""):
        self.topic_name = topic_name
        self.description = description
        self.subscribers: Set[str] = set()
        self.message_history: List[AgentMessage] = []
        self.created_at = datetime.utcnow()
        self.is_active = True
        self._callbacks: Dict[str, List[Callable]] = defaultdict(list)

    def subscribe(self, agent_name: str, callback: Optional[Callable] = None) -> None:
        """Subscribe an agent to this topic channel."""
        self.subscribers.add(agent_name)
        if callback:
            self._callbacks[agent_name].append(callback)
        logger.info(f"Agent '{agent_name}' subscribed to topic '{self.topic_name}'")

    def unsubscribe(self, agent_name: str) -> None:
        """Unsubscribe an agent from this topic channel."""
        self.subscribers.discard(agent_name)
        self._callbacks.pop(agent_name, None)
        logger.info(f"Agent '{agent_name}' unsubscribed from topic '{self.topic_name}'")

    async def publish(self, message: AgentMessage) -> None:
        """
        Publish a message to all subscribers on this topic channel.
        Triggers registered callbacks for each subscriber.
        """
        self.message_history.append(message)
        logger.debug(
            f"[{self.topic_name}] {message.sender} → {message.receiver}: "
            f"{message.message_type} ({len(message.content)} chars)"
        )
        # Notify subscribers via callbacks
        for agent_name, callbacks in self._callbacks.items():
            if agent_name != message.sender:
                for cb in callbacks:
                    try:
                        if asyncio.iscoroutinefunction(cb):
                            await cb(message)
                        else:
                            cb(message)
                    except Exception as e:
                        logger.error(f"Callback error for {agent_name}: {e}")

    def get_recent_messages(self, n: int = 10) -> List[AgentMessage]:
        """Retrieve the most recent N messages from this channel."""
        return self.message_history[-n:]


# ─── TB-CSPN Manager ────────────────────────────────────────────────────────

class TBCSPN:
    """
    Topic-Based Communication-Space Petri Net Manager.
    
    Coordinates multi-agent communication through dynamically created
    topic channels, manages workflow state via Petri Net tokens, and
    triggers human-in-the-loop interventions when confidence drops
    below threshold.
    
    Usage:
        cspn = TBCSPN()
        cspn.create_topic("task_planning", "Coordinates task decomposition")
        cspn.create_topic("browser_execution", "Manages browser actions")
        cspn.create_topic("verification", "Handles output verification")
        
        token = cspn.create_token("task_001", belief_state)
        await cspn.transition(token, PlaceType.PLANNING)
    """

    def __init__(
        self,
        human_confidence_threshold: float = 0.3,
        max_replanning_cycles: int = 5
    ):
        self.topics: Dict[str, TopicChannel] = {}
        self.tokens: Dict[str, Token] = {}
        self.transitions: List[Transition] = []
        self.human_confidence_threshold = human_confidence_threshold
        self.max_replanning_cycles = max_replanning_cycles
        self._human_callback: Optional[Callable] = None
        self._setup_default_transitions()

    def _setup_default_transitions(self) -> None:
        """Initialize the standard workflow transitions for the Petri Net."""

        # Idle → Planning: When a new task arrives
        self.transitions.append(Transition(
            name="start_planning",
            input_places=[PlaceType.IDLE],
            output_places=[PlaceType.PLANNING],
            priority=1
        ))

        # Planning → Executing: When plan is ready
        self.transitions.append(Transition(
            name="begin_execution",
            input_places=[PlaceType.PLANNING],
            output_places=[PlaceType.EXECUTING],
            guard=lambda t: len(t.belief_state.plan_history) > 0,
            priority=2
        ))

        # Executing → Verifying: After browser action completes
        self.transitions.append(Transition(
            name="verify_action",
            input_places=[PlaceType.EXECUTING],
            output_places=[PlaceType.VERIFYING],
            guard=lambda t: len(t.belief_state.action_history) > 0,
            priority=2
        ))

        # Verifying → Completed: Task verified successfully
        self.transitions.append(Transition(
            name="task_complete",
            input_places=[PlaceType.VERIFYING],
            output_places=[PlaceType.COMPLETED],
            guard=lambda t: t.belief_state.epistemic_confidence >= 0.8,
            priority=3
        ))

        # Verifying → Replanning: Verification failed, need new plan
        self.transitions.append(Transition(
            name="replan",
            input_places=[PlaceType.VERIFYING],
            output_places=[PlaceType.REPLANNING],
            guard=lambda t: t.belief_state.epistemic_confidence < 0.8 and t.belief_state.error_count < 3,
            priority=2
        ))

        # Replanning → Planning: Generate revised plan
        self.transitions.append(Transition(
            name="revise_plan",
            input_places=[PlaceType.REPLANNING],
            output_places=[PlaceType.PLANNING],
            priority=1
        ))

        # Any → AwaitingHuman: Confidence dropped below threshold
        for place in [PlaceType.EXECUTING, PlaceType.VERIFYING, PlaceType.REPLANNING]:
            self.transitions.append(Transition(
                name=f"request_human_from_{place.value}",
                input_places=[place],
                output_places=[PlaceType.AWAITING_HUMAN],
                guard=lambda t: (
                    t.belief_state.epistemic_confidence < self.human_confidence_threshold
                    or t.belief_state.error_count >= t.belief_state.max_errors_before_human
                ),
                priority=10  # High priority — safety net
            ))

        # AwaitingHuman → Planning: After human provides feedback
        self.transitions.append(Transition(
            name="resume_after_human",
            input_places=[PlaceType.AWAITING_HUMAN],
            output_places=[PlaceType.PLANNING],
            guard=lambda t: len(t.belief_state.human_feedback) > 0,
            priority=5
        ))

    # ─── Topic Management ────────────────────────────────────────────────

    def create_topic(self, topic_name: str, description: str = "") -> TopicChannel:
        """Create a new topic channel for agent communication."""
        if topic_name in self.topics:
            logger.warning(f"Topic '{topic_name}' already exists, returning existing channel")
            return self.topics[topic_name]
        channel = TopicChannel(topic_name, description)
        self.topics[topic_name] = channel
        logger.info(f"Created topic channel: '{topic_name}'")
        return channel

    def get_topic(self, topic_name: str) -> Optional[TopicChannel]:
        """Retrieve a topic channel by name."""
        return self.topics.get(topic_name)

    def list_active_topics(self) -> List[str]:
        """List all currently active topic channel names."""
        return [name for name, ch in self.topics.items() if ch.is_active]

    # ─── Token Management ────────────────────────────────────────────────

    def create_token(
        self,
        token_id: str,
        belief_state: BeliefState,
        initial_place: PlaceType = PlaceType.IDLE
    ) -> Token:
        """Create a new token positioned at the given place."""
        token = Token(token_id, initial_place, belief_state)
        self.tokens[token_id] = token
        logger.info(f"Created token '{token_id}' at place '{initial_place.value}'")
        return token

    def get_token(self, token_id: str) -> Optional[Token]:
        """Retrieve a token by ID."""
        return self.tokens.get(token_id)

    # ─── Transition Engine ───────────────────────────────────────────────

    async def try_fire(self, token: Token) -> Optional[PlaceType]:
        """
        Attempt to fire the highest-priority enabled transition for a token.
        Returns the new place if a transition fired, None otherwise.
        """
        # Sort transitions by priority (descending) for deterministic firing
        enabled = [t for t in self.transitions if t.is_enabled(token)]
        if not enabled:
            logger.debug(f"No enabled transitions for token '{token.token_id}' at '{token.place.value}'")
            return None

        enabled.sort(key=lambda t: t.priority, reverse=True)
        transition = enabled[0]
        new_place = await transition.fire(token)
        logger.info(
            f"Fired transition '{transition.name}': "
            f"{token.transition_history[-1]['from']} → {new_place.value}"
        )

        # Check if human intervention is needed
        if new_place == PlaceType.AWAITING_HUMAN and self._human_callback:
            await self._request_human_intervention(token)

        return new_place

    async def transition(self, token: Token, target_place: PlaceType) -> None:
        """
        Explicitly transition a token to a target place.
        Used for direct state management outside the Petri Net engine.
        """
        token.move_to(target_place, reason="explicit_transition")
        logger.info(f"Explicit transition: token '{token.token_id}' → {target_place.value}")

    # ─── Human-in-the-Loop ───────────────────────────────────────────────

    def set_human_callback(self, callback: Callable) -> None:
        """Register a callback for human-in-the-loop intervention."""
        self._human_callback = callback

    async def _request_human_intervention(self, token: Token) -> None:
        """Trigger human intervention when confidence drops below threshold."""
        logger.warning(
            f"Human intervention requested for token '{token.token_id}'. "
            f"Confidence: {token.belief_state.epistemic_confidence:.2f}, "
            f"Errors: {token.belief_state.error_count}"
        )
        if self._human_callback:
            if asyncio.iscoroutinefunction(self._human_callback):
                await self._human_callback(token)
            else:
                self._human_callback(token)

    def provide_human_feedback(self, token_id: str, feedback: str) -> None:
        """Inject human feedback into a token's belief state."""
        token = self.tokens.get(token_id)
        if token:
            token.belief_state.human_feedback.append(feedback)
            token.belief_state.epistemic_confidence = min(
                1.0, token.belief_state.epistemic_confidence + 0.3
            )
            logger.info(f"Human feedback received for token '{token_id}'")

    # ─── Messaging ───────────────────────────────────────────────────────

    async def send_message(
        self,
        topic_name: str,
        message: AgentMessage
    ) -> None:
        """Send a structured message through a topic channel."""
        channel = self.topics.get(topic_name)
        if channel:
            await channel.publish(message)
        else:
            logger.error(f"Topic '{topic_name}' not found. Creating dynamically.")
            channel = self.create_topic(topic_name, "Auto-created topic")
            await channel.publish(message)

    # ─── Diagnostics ─────────────────────────────────────────────────────

    def get_network_state(self) -> Dict[str, Any]:
        """Return a snapshot of the entire Petri Net state for debugging."""
        return {
            "tokens": {
                tid: {
                    "place": t.place.value,
                    "confidence": t.belief_state.epistemic_confidence,
                    "errors": t.belief_state.error_count,
                    "transitions": len(t.transition_history)
                }
                for tid, t in self.tokens.items()
            },
            "topics": {
                name: {
                    "subscribers": list(ch.subscribers),
                    "message_count": len(ch.message_history),
                    "is_active": ch.is_active
                }
                for name, ch in self.topics.items()
            },
            "transition_fire_counts": {
                t.name: t.fire_count for t in self.transitions
            }
        }
