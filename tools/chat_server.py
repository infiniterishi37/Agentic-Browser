"""
WebSocket Chat Server
=====================

Lightweight WebSocket server that bridges the browser chat widget
with the Python agent loop via an asyncio.Queue.
"""

import asyncio
import json
import logging
import os
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class ChatServer:
    """
    WebSocket server running on localhost for browser ↔ agent communication.
    
    The browser chat widget connects and sends user messages.
    The agent loop reads from the message queue and can push
    responses/status updates back to the browser.
    """

    def __init__(self, host: str = "localhost", port: int = 8765):
        self.host = host
        self.port = port
        self.message_queue: asyncio.Queue = asyncio.Queue()
        self._server = None
        self._connections: set = set()
        self.max_history = 300
        self.history: list[Dict[str, Any]] = []
        # Shared chat panel UI state across all tabs/pages.
        self.panel_open: bool = True
        self.selected_provider: str = os.getenv("LLM_PROVIDER", "google").lower().strip()
        if self.selected_provider not in {"google", "groq"}:
            self.selected_provider = "google"
        self.selected_model: str = (
            os.getenv("MODEL", "gemini-2.5-flash-lite")
            if self.selected_provider == "google"
            else os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
        )
        self.loop_limit: int = int(os.getenv("AGENT_LOOP_LIMIT", "3"))
        if self.loop_limit < 1:
            self.loop_limit = 3
        self.agent_state: Dict[str, Any] = {
            "running": False,
            "run_id": "",
            "provider": self.selected_provider,
            "model": self.selected_model,
            "loop_limit": self.loop_limit,
            "last_error": "",
        }

    async def start(self) -> None:
        """Start the WebSocket server in the background."""
        try:
            import websockets
            self._server = await websockets.serve(
                self._handler, self.host, self.port
            )
            logger.info(f"Chat WebSocket server started on ws://{self.host}:{self.port}")
            print(f"💬 Chat server running on ws://{self.host}:{self.port}")
        except ImportError:
            logger.warning("websockets package not installed. Chat server disabled.")
            print("⚠️ Chat server disabled (install 'websockets' package to enable)")
        except Exception as e:
            logger.error(f"Failed to start chat server: {e}")
            print(f"⚠️ Chat server failed to start: {e}")

    async def _handler(self, websocket, path=None) -> None:
        """Handle incoming WebSocket connections."""
        self._connections.add(websocket)
        client_id = id(websocket)
        logger.info(f"Chat client connected: {client_id}")

        # Send connection + shared state snapshot
        await websocket.send(json.dumps({
            "type": "system",
            "content": "Connected to Agentic Browser. Send me a task!",
        }))
        await websocket.send(json.dumps({
            "type": "history",
            "messages": self.history,
        }))
        await websocket.send(json.dumps({
            "type": "ui_state",
            "panel_open": self.panel_open,
            "provider": self.selected_provider,
            "model": self.selected_model,
            "loop_limit": self.loop_limit,
        }))
        await websocket.send(json.dumps({
            "type": "agent_state",
            **self.agent_state,
        }))

        try:
            async for raw_message in websocket:
                try:
                    data = json.loads(raw_message)
                    msg_type = data.get("type", "")

                    # UI state sync from browser widget.
                    if msg_type == "ui_state":
                        self.panel_open = bool(data.get("panel_open", False))
                        provider = (data.get("provider") or "").strip().lower()
                        model = (data.get("model") or "").strip()
                        loop_limit_raw = data.get("loop_limit")
                        loop_limit = self.loop_limit
                        if isinstance(loop_limit_raw, (int, float, str)):
                            try:
                                parsed = int(loop_limit_raw)
                                if parsed >= 1:
                                    loop_limit = parsed
                            except Exception:
                                pass
                        if provider in {"google", "groq"}:
                            self.selected_provider = provider
                        if model:
                            self.selected_model = model
                        self.loop_limit = loop_limit
                        self.agent_state["loop_limit"] = self.loop_limit
                        await self._broadcast_payload({
                            "type": "ui_state",
                            "panel_open": self.panel_open,
                            "provider": self.selected_provider,
                            "model": self.selected_model,
                            "loop_limit": self.loop_limit,
                        })
                        continue

                    content = data.get("content", "").strip()
                    if content:
                        provider = (data.get("provider") or self.selected_provider).strip().lower()
                        model = (data.get("model") or self.selected_model).strip()
                        loop_limit_raw = data.get("loop_limit", self.loop_limit)
                        loop_limit = self.loop_limit
                        if isinstance(loop_limit_raw, (int, float, str)):
                            try:
                                parsed = int(loop_limit_raw)
                                if parsed >= 1:
                                    loop_limit = parsed
                            except Exception:
                                pass
                        if provider in {"google", "groq"}:
                            self.selected_provider = provider
                        if model:
                            self.selected_model = model
                        self.loop_limit = loop_limit
                        self.agent_state["loop_limit"] = self.loop_limit

                        logger.info(
                            "Chat message from browser: %s | provider=%s | model=%s | loop_limit=%s",
                            content,
                            self.selected_provider,
                            self.selected_model,
                            self.loop_limit,
                        )
                        # Queue as dict with optional provider/model
                        msg_data = {
                            "content": content,
                            "provider": self.selected_provider,
                            "model": self.selected_model,
                            "loop_limit": self.loop_limit,
                        }
                        await self._broadcast_payload({
                            "type": "user",
                            "content": content,
                        })
                        await self.message_queue.put(msg_data)
                        # Acknowledge receipt
                        provider_label = msg_data["provider"]
                        model_label = msg_data["model"]
                        await websocket.send(json.dumps({
                            "type": "status",
                            "content": f"🧠 Processing with {provider_label}/{model_label}...",
                        }))
                        await self._broadcast_payload({
                            "type": "ui_state",
                            "panel_open": self.panel_open,
                            "provider": self.selected_provider,
                            "model": self.selected_model,
                            "loop_limit": self.loop_limit,
                        })
                except json.JSONDecodeError:
                    # Treat as plain text
                    text = raw_message.strip()
                    if text:
                        await self._broadcast_payload({"type": "user", "content": text})
                        await self.message_queue.put({
                            "content": text,
                            "provider": self.selected_provider,
                            "model": self.selected_model,
                            "loop_limit": self.loop_limit,
                        })
        except Exception as e:
            logger.debug(f"Chat client {client_id} disconnected: {e}")
        finally:
            self._connections.discard(websocket)

    async def send_to_browser(self, message: str, msg_type: str = "response") -> None:
        """Push a message to all connected browser chat widgets."""
        payload = {"type": msg_type, "content": message}
        await self._broadcast_payload(payload)

    async def _broadcast_payload(self, payload: Dict[str, Any]) -> None:
        """Broadcast raw JSON payload to all connected browser widgets."""
        self._record_history(payload)
        encoded = json.dumps(payload)
        disconnected = set()
        for ws in self._connections:
            try:
                await ws.send(encoded)
            except Exception:
                disconnected.add(ws)
        self._connections -= disconnected

    def _record_history(self, payload: Dict[str, Any]) -> None:
        """Store durable chat history entries."""
        msg_type = payload.get("type", "")
        # Keep only durable conversation messages in history.
        if msg_type not in {"user", "agent", "response", "system", "status"}:
            return
        content = str(payload.get("content", "")).strip()
        if not content:
            return
        entry = {"type": msg_type, "content": content}
        self.history.append(entry)
        if len(self.history) > self.max_history:
            self.history = self.history[-self.max_history :]

    async def update_agent_state(
        self,
        running: bool,
        run_id: str = "",
        provider: str = "",
        model: str = "",
        loop_limit: int | None = None,
        last_error: str = "",
    ) -> None:
        """Update and broadcast current shared agent state."""
        self.agent_state["running"] = bool(running)
        self.agent_state["run_id"] = run_id or self.agent_state.get("run_id", "")
        if provider:
            self.agent_state["provider"] = provider
        if model:
            self.agent_state["model"] = model
        if loop_limit is not None and loop_limit >= 1:
            self.agent_state["loop_limit"] = int(loop_limit)
        if last_error or not running:
            self.agent_state["last_error"] = last_error
        await self._broadcast_payload({
            "type": "agent_state",
            **self.agent_state,
        })

    async def get_message(self, timeout: float = 0.1) -> Optional[Dict[str, Any]]:
        """Non-blocking read from the message queue.

        Returns a dict with keys: content, provider, model.
        """
        try:
            return await asyncio.wait_for(self.message_queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    async def stop(self) -> None:
        """Shut down the WebSocket server."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            logger.info("Chat server stopped")


# Global instance
chat_server = ChatServer()
