"""
Unified State Management Abstraction Layer
===========================================

Provides consistent state access regardless of underlying orchestration framework.
Normalizes state management across:
  - AutoGen: conversation history + GroupChat context
  - LangGraph: TypedDict persisted across graph nodes
  - CrewAI: distributed crew memories + task outputs

Integrates Redis for session state, PostgreSQL for task history,
and file system for execution artifacts.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from orchestration.models import (
    ActionResult,
    BeliefState,
    ExecutionTrace,
    TaskPlan,
    TaskStatus,
    VerificationResult,
)

logger = logging.getLogger(__name__)


# ─── Storage Backends ────────────────────────────────────────────────────────

class RedisSessionStore:
    """
    Redis-backed session state for real-time agent state sharing.
    Stores current plan, action history, and belief state.
    Persisted across agent invocations within a session.
    """

    def __init__(self, redis_url: str = "redis://localhost:6379/0"):
        self.redis_url = redis_url
        self._client = None
        self._fallback_store: Dict[str, Any] = {}

    async def connect(self) -> None:
        """Establish connection to Redis server."""
        try:
            import redis.asyncio as aioredis
            self._client = aioredis.from_url(
                self.redis_url,
                encoding="utf-8",
                decode_responses=True
            )
            await self._client.ping()
            logger.info(f"Connected to Redis at {self.redis_url}")
        except Exception as e:
            logger.warning(f"Redis unavailable ({e}), using in-memory fallback")
            self._client = None

    async def get(self, key: str) -> Optional[str]:
        """Retrieve a value by key."""
        if self._client:
            try:
                return await self._client.get(key)
            except Exception:
                pass
        return self._fallback_store.get(key)

    async def set(self, key: str, value: str, ttl: int = 3600) -> None:
        """Store a key-value pair with TTL."""
        if self._client:
            try:
                await self._client.setex(key, ttl, value)
                return
            except Exception:
                pass
        self._fallback_store[key] = value

    async def delete(self, key: str) -> None:
        """Remove a key from the store."""
        if self._client:
            try:
                await self._client.delete(key)
                return
            except Exception:
                pass
        self._fallback_store.pop(key, None)

    async def close(self) -> None:
        """Close the Redis connection."""
        if self._client:
            await self._client.close()


class PostgresTaskStore:
    """
    PostgreSQL-backed task history for complete execution traces.
    Stores all agent communications, tool invocations, and verification results.
    Enables post-execution analysis and system improvement.
    """

    def __init__(self, database_url: str = "postgresql://localhost:5432/agentic_browser"):
        self.database_url = database_url
        self._pool = None
        self._fallback_traces: Dict[str, Dict] = {}

    async def connect(self) -> None:
        """Create connection pool to PostgreSQL."""
        try:
            import asyncpg
            self._pool = await asyncpg.create_pool(
                self.database_url,
                min_size=2,
                max_size=10
            )
            await self._initialize_schema()
            logger.info(f"Connected to PostgreSQL at {self.database_url}")
        except Exception as e:
            logger.warning(f"PostgreSQL unavailable ({e}), using in-memory fallback")
            self._pool = None

    async def _initialize_schema(self) -> None:
        """Create tables if they don't exist."""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS execution_traces (
                    trace_id VARCHAR(64) PRIMARY KEY,
                    task_plan JSONB NOT NULL,
                    action_results JSONB DEFAULT '[]',
                    verification_results JSONB DEFAULT '[]',
                    total_execution_time FLOAT DEFAULT 0,
                    total_tokens_consumed INT DEFAULT 0,
                    total_steps INT DEFAULT 0,
                    reasoning_steps INT DEFAULT 0,
                    tool_steps INT DEFAULT 0,
                    replanning_cycles INT DEFAULT 0,
                    human_interventions INT DEFAULT 0,
                    final_status VARCHAR(32) DEFAULT 'pending',
                    started_at TIMESTAMPTZ DEFAULT NOW(),
                    completed_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
                
                CREATE TABLE IF NOT EXISTS agent_messages (
                    id SERIAL PRIMARY KEY,
                    trace_id VARCHAR(64) REFERENCES execution_traces(trace_id),
                    sender VARCHAR(32) NOT NULL,
                    receiver VARCHAR(32) NOT NULL,
                    content TEXT NOT NULL,
                    message_type VARCHAR(32) DEFAULT 'action',
                    timestamp TIMESTAMPTZ DEFAULT NOW(),
                    metadata JSONB DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_traces_status 
                    ON execution_traces(final_status);
                CREATE INDEX IF NOT EXISTS idx_messages_trace 
                    ON agent_messages(trace_id);
            """)

    async def save_trace(self, trace: ExecutionTrace) -> None:
        """Persist a complete execution trace."""
        trace_data = trace.model_dump(mode="json")
        if self._pool:
            try:
                async with self._pool.acquire() as conn:
                    await conn.execute("""
                        INSERT INTO execution_traces 
                            (trace_id, task_plan, action_results, verification_results,
                             total_execution_time, total_tokens_consumed, total_steps,
                             reasoning_steps, tool_steps, replanning_cycles,
                             human_interventions, final_status, started_at, completed_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
                        ON CONFLICT (trace_id) DO UPDATE SET
                            action_results = $3,
                            verification_results = $4,
                            total_execution_time = $5,
                            total_tokens_consumed = $6,
                            final_status = $12,
                            completed_at = $14
                    """,
                        trace.trace_id,
                        json.dumps(trace_data["task_plan"]),
                        json.dumps(trace_data["action_results"]),
                        json.dumps(trace_data["verification_results"]),
                        trace.total_execution_time,
                        trace.total_tokens_consumed,
                        trace.total_steps,
                        trace.reasoning_steps,
                        trace.tool_steps,
                        trace.replanning_cycles,
                        trace.human_interventions,
                        trace.final_status.value,
                        trace.started_at,
                        trace.completed_at,
                    )
                return
            except Exception as e:
                logger.error(f"Failed to save trace to PostgreSQL: {e}")
        # Fallback to in-memory
        self._fallback_traces[trace.trace_id] = trace_data

    async def get_trace(self, trace_id: str) -> Optional[Dict]:
        """Retrieve an execution trace by ID."""
        if self._pool:
            try:
                async with self._pool.acquire() as conn:
                    row = await conn.fetchrow(
                        "SELECT * FROM execution_traces WHERE trace_id = $1",
                        trace_id
                    )
                    if row:
                        return dict(row)
            except Exception:
                pass
        return self._fallback_traces.get(trace_id)

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()


class FileArtifactStore:
    """
    File system storage for execution artifacts: screenshots,
    DOM snapshots, extracted data, and debug logs.
    """

    def __init__(self, base_dir: str = "./artifacts"):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def save_artifact(
        self,
        trace_id: str,
        filename: str,
        content: str | bytes,
        subdir: str = ""
    ) -> Path:
        """Save an artifact file and return its path."""
        artifact_dir = self.base_dir / trace_id / subdir
        artifact_dir.mkdir(parents=True, exist_ok=True)
        filepath = artifact_dir / filename
        mode = "wb" if isinstance(content, bytes) else "w"
        with open(filepath, mode) as f:
            f.write(content)
        logger.debug(f"Saved artifact: {filepath}")
        return filepath

    def load_artifact(self, trace_id: str, filename: str, subdir: str = "") -> Optional[str]:
        """Load an artifact file's contents."""
        filepath = self.base_dir / trace_id / subdir / filename
        if filepath.exists():
            return filepath.read_text()
        return None


# ─── Unified State Manager ──────────────────────────────────────────────────

class StateManager:
    """
    Unified state management layer that normalizes state access
    across AutoGen, LangGraph, and CrewAI orchestration frameworks.
    
    Provides a single interface for:
      - Session state (Redis) — current plan, real-time agent state
      - Task history (PostgreSQL) — complete execution traces
      - Artifacts (file system) — screenshots, DOM snapshots, data
    
    Usage:
        state_mgr = StateManager()
        await state_mgr.initialize()
        
        # Create a new execution session
        session = await state_mgr.create_session("user_task_123", belief_state)
        
        # Update state during execution
        await state_mgr.update_belief_state(session_id, updated_state)
        await state_mgr.record_action(session_id, action_result)
        
        # Finalize and persist
        await state_mgr.finalize_session(session_id)
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379/0",
        postgres_url: str = "postgresql://localhost:5432/agentic_browser",
        artifacts_dir: str = "./artifacts"
    ):
        self.redis = RedisSessionStore(redis_url)
        self.postgres = PostgresTaskStore(postgres_url)
        self.artifacts = FileArtifactStore(artifacts_dir)
        self._sessions: Dict[str, Dict[str, Any]] = {}

    async def initialize(self) -> None:
        """Initialize all storage backends."""
        await self.redis.connect()
        await self.postgres.connect()
        logger.info("StateManager initialized with all storage backends")

    async def create_session(
        self,
        session_id: str,
        belief_state: BeliefState,
        framework: str = "langgraph_autogen"
    ) -> str:
        """
        Create a new execution session with initial belief state.
        Returns session_id for future state operations.
        """
        session_data = {
            "session_id": session_id,
            "framework": framework,
            "belief_state": belief_state.model_dump(mode="json"),
            "execution_trace": None,
            "started_at": datetime.utcnow().isoformat(),
            "status": TaskStatus.PENDING.value,
        }
        self._sessions[session_id] = session_data
        await self.redis.set(
            f"session:{session_id}",
            json.dumps(session_data),
            ttl=7200
        )
        logger.info(f"Created session '{session_id}' with framework '{framework}'")
        return session_id

    async def get_belief_state(self, session_id: str) -> Optional[BeliefState]:
        """Retrieve the current belief state for a session."""
        session = self._sessions.get(session_id)
        if session:
            return BeliefState(**session["belief_state"])
        # Try Redis
        data = await self.redis.get(f"session:{session_id}")
        if data:
            session = json.loads(data)
            return BeliefState(**session["belief_state"])
        return None

    async def update_belief_state(
        self,
        session_id: str,
        belief_state: BeliefState
    ) -> None:
        """Update the belief state for a session."""
        if session_id in self._sessions:
            self._sessions[session_id]["belief_state"] = belief_state.model_dump(mode="json")
        await self.redis.set(
            f"session:{session_id}:belief",
            belief_state.model_dump_json(),
            ttl=7200
        )

    async def record_action(
        self,
        session_id: str,
        action_result: ActionResult
    ) -> None:
        """Record a browser action result in the session state."""
        session = self._sessions.get(session_id, {})
        actions = session.setdefault("action_results", [])
        actions.append(action_result.model_dump(mode="json"))
        await self.redis.set(
            f"session:{session_id}:last_action",
            action_result.model_dump_json(),
            ttl=7200
        )

    async def record_verification(
        self,
        session_id: str,
        verification: VerificationResult
    ) -> None:
        """Record a verification result in the session state."""
        session = self._sessions.get(session_id, {})
        verifications = session.setdefault("verification_results", [])
        verifications.append(verification.model_dump(mode="json"))

    async def record_plan(
        self,
        session_id: str,
        plan: TaskPlan
    ) -> None:
        """Record a task plan in the session state."""
        session = self._sessions.get(session_id, {})
        plans = session.setdefault("plans", [])
        plans.append(plan.model_dump(mode="json"))
        await self.redis.set(
            f"session:{session_id}:current_plan",
            plan.model_dump_json(),
            ttl=7200
        )

    async def save_screenshot(
        self,
        session_id: str,
        screenshot_data: bytes,
        step_id: str = ""
    ) -> str:
        """Save a screenshot artifact and return its path."""
        filename = f"screenshot_{step_id}_{int(time.time())}.png"
        path = self.artifacts.save_artifact(
            session_id, filename, screenshot_data, subdir="screenshots"
        )
        return str(path)

    async def finalize_session(
        self,
        session_id: str,
        trace: ExecutionTrace
    ) -> None:
        """Persist the complete execution trace to PostgreSQL."""
        await self.postgres.save_trace(trace)
        if session_id in self._sessions:
            self._sessions[session_id]["status"] = trace.final_status.value
        await self.redis.delete(f"session:{session_id}")
        logger.info(f"Session '{session_id}' finalized with status '{trace.final_status.value}'")

    async def shutdown(self) -> None:
        """Clean up all storage connections."""
        await self.redis.close()
        await self.postgres.close()
        logger.info("StateManager shut down cleanly")
