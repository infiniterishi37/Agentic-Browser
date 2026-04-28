import asyncio
from typing import Optional, List, Dict
from playwright.async_api import (
    async_playwright,
    Page,
    Browser as PlaywrightBrowser,
    BrowserContext,
)
from bs4 import BeautifulSoup
import html2text

# Chat widget HTML/CSS/JS injected into browser pages
CHAT_WIDGET_JS = """
(() => {
    const existingRoot = document.getElementById('agentic-chat-root');
    if (existingRoot) return;

    const root = document.createElement('div');
    root.id = 'agentic-chat-root';
    root.innerHTML = `
    <style>
        #agentic-chat-root {
            all: initial;
            position: fixed;
            right: 24px;
            bottom: 24px;
            z-index: 2147483645;
            pointer-events: none;
        }
        #agentic-chat-root, #agentic-chat-root * {
            box-sizing: border-box;
        }
        #agentic-chat-bubble {
            position: absolute;
            right: 0;
            bottom: 0;
            width: 56px;
            height: 56px;
            border-radius: 50%;
            background: linear-gradient(135deg, #6366f1, #8b5cf6, #a855f7);
            box-shadow: 0 4px 24px rgba(99,102,241,0.45), 0 0 0 3px rgba(139,92,246,0.15);
            display: flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            z-index: 2147483647;
            pointer-events: auto;
            transition: transform 0.25s cubic-bezier(.4,2,.6,1), box-shadow 0.25s ease;
            user-select: none;
        }
        #agentic-chat-bubble:hover {
            transform: scale(1.12);
            box-shadow: 0 6px 32px rgba(99,102,241,0.55), 0 0 0 5px rgba(139,92,246,0.2);
        }
        #agentic-chat-bubble svg {
            width: 26px; height: 26px; fill: white;
        }
        #agentic-chat-panel {
            position: absolute;
            right: 0;
            bottom: 68px;
            width: min(380px, calc(100vw - 20px));
            max-height: min(540px, calc(100vh - 120px));
            background: rgba(15, 15, 25, 0.92);
            backdrop-filter: blur(20px) saturate(1.6);
            -webkit-backdrop-filter: blur(20px) saturate(1.6);
            border-radius: 18px;
            border: 1px solid rgba(139, 92, 246, 0.25);
            box-shadow: 0 8px 48px rgba(0,0,0,0.5), 0 0 0 1px rgba(255,255,255,0.05) inset;
            display: none;
            flex-direction: column;
            overflow: hidden;
            z-index: 2147483646;
            pointer-events: auto;
            font-family: 'Inter', 'Segoe UI', -apple-system, sans-serif;
            animation: agentic-slide-up 0.3s cubic-bezier(.22,1,.36,1);
        }
        @media (max-width: 520px) {
            #agentic-chat-root {
                right: 10px;
                bottom: 14px;
            }
            #agentic-chat-bubble {
                right: 0;
                bottom: 0;
            }
            #agentic-chat-panel {
                right: 0;
                bottom: 80px;
                width: calc(100vw - 20px);
            }
        }
        @keyframes agentic-slide-up {
            from { opacity: 0; transform: translateY(16px) scale(0.96); }
            to { opacity: 1; transform: translateY(0) scale(1); }
        }
        #agentic-chat-panel.open { display: flex; }
        #agentic-chat-header {
            padding: 14px 18px;
            background: linear-gradient(135deg, rgba(99,102,241,0.15), rgba(139,92,246,0.1));
            border-bottom: 1px solid rgba(139,92,246,0.15);
            display: flex;
            align-items: center;
            gap: 10px;
        }
        #agentic-chat-header .dot {
            width: 9px; height: 9px; border-radius: 50%;
            background: #34d399;
            box-shadow: 0 0 8px rgba(52,211,153,0.5);
            animation: agentic-pulse 2s ease-in-out infinite;
        }
        @keyframes agentic-pulse {
            0%, 100% { opacity: 1; } 50% { opacity: 0.5; }
        }
        #agentic-chat-header span {
            color: #e0e0ff;
            font-size: 14px;
            font-weight: 600;
            letter-spacing: 0.3px;
        }
        #agentic-chat-messages {
            flex: 1;
            overflow-y: auto;
            padding: 14px;
            display: flex;
            flex-direction: column;
            gap: 10px;
            min-height: 200px;
            max-height: 320px;
            scrollbar-width: thin;
            scrollbar-color: rgba(139,92,246,0.3) transparent;
        }
        #agentic-chat-messages::-webkit-scrollbar { width: 5px; }
        #agentic-chat-messages::-webkit-scrollbar-thumb {
            background: rgba(139,92,246,0.3); border-radius: 10px;
        }
        .agentic-chat-section {
            border-bottom: 1px solid rgba(139,92,246,0.12);
            background: rgba(0,0,0,0.12);
        }
        .agentic-chat-toggle {
            width: 100%;
            border: none;
            background: transparent;
            color: #c9c9f9;
            font-size: 12px;
            font-weight: 600;
            letter-spacing: 0.3px;
            text-align: left;
            padding: 9px 14px;
            cursor: pointer;
        }
        .agentic-chat-section-body {
            display: block;
        }
        #agentic-chat-state-wrap {
            display: none;
            max-height: 130px;
            overflow: auto;
            padding: 0 14px 12px;
        }
        #agentic-chat-state {
            margin: 0;
            color: #c4f1d4;
            font-size: 11px;
            line-height: 1.45;
            white-space: pre-wrap;
            word-break: break-word;
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
        }
        .agentic-msg {
            padding: 10px 14px;
            border-radius: 14px;
            font-size: 13px;
            line-height: 1.5;
            max-width: 85%;
            word-wrap: break-word;
            animation: agentic-msg-in 0.2s ease;
        }
        @keyframes agentic-msg-in {
            from { opacity: 0; transform: translateY(6px); }
            to { opacity: 1; transform: translateY(0); }
        }
        .agentic-msg.user {
            align-self: flex-end;
            background: linear-gradient(135deg, #6366f1, #8b5cf6);
            color: white;
            border-bottom-right-radius: 4px;
        }
        .agentic-msg.agent {
            align-self: flex-start;
            background: rgba(255,255,255,0.08);
            color: #d4d4e8;
            border: 1px solid rgba(139,92,246,0.12);
            border-bottom-left-radius: 4px;
        }
        .agentic-msg.system {
            align-self: center;
            background: rgba(52,211,153,0.1);
            color: #86efac;
            border: 1px solid rgba(52,211,153,0.15);
            font-size: 12px;
            text-align: center;
        }
        .agentic-msg.status {
            align-self: center;
            background: rgba(250,204,21,0.1);
            color: #fde68a;
            border: 1px solid rgba(250,204,21,0.15);
            font-size: 12px;
            text-align: center;
        }
        #agentic-chat-input-area {
            padding: 12px;
            border-top: 1px solid rgba(139,92,246,0.12);
            display: flex;
            flex-direction: column;
            gap: 8px;
            background: rgba(0,0,0,0.2);
        }
        #agentic-chat-controls {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 8px;
        }
        .agentic-chat-select {
            width: 100%;
            height: 34px;
            border-radius: 10px;
            border: 1px solid rgba(139,92,246,0.2);
            background: rgba(255,255,255,0.07);
            color: #e0e0ff;
            font-size: 12px;
            padding: 0 10px;
            outline: none;
        }
        .agentic-chat-select:focus {
            border-color: rgba(139,92,246,0.5);
            box-shadow: 0 0 0 2px rgba(139,92,246,0.1);
        }
        #agentic-chat-composer {
            display: flex;
            align-items: center;
            gap: 8px;
        }
        #agentic-chat-footer {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 10px;
            margin-top: 2px;
        }
        #agentic-chat-loop-wrap {
            display: flex;
            align-items: center;
            gap: 6px;
        }
        #agentic-chat-loop-wrap label {
            color: #c8c8ef;
            font-size: 11px;
        }
        #agentic-chat-loop-limit {
            width: 56px;
            height: 28px;
            border-radius: 8px;
            border: 1px solid rgba(139,92,246,0.25);
            background: rgba(255,255,255,0.07);
            color: #e0e0ff;
            font-size: 12px;
            padding: 0 8px;
            outline: none;
        }
        #agentic-chat-input {
            flex: 1;
            height: 40px;
            background: rgba(255,255,255,0.07);
            border: 1px solid rgba(139,92,246,0.2);
            border-radius: 12px;
            padding: 0 14px;
            color: #e0e0ff;
            font-size: 13px;
            font-family: inherit;
            outline: none;
            transition: border-color 0.2s, box-shadow 0.2s;
        }
        #agentic-chat-input:focus {
            border-color: rgba(139,92,246,0.5);
            box-shadow: 0 0 0 3px rgba(139,92,246,0.1);
        }
        #agentic-chat-input::placeholder { color: rgba(200,200,230,0.35); }
        #agentic-chat-send {
            width: 40px; height: 40px;
            border-radius: 12px;
            background: linear-gradient(135deg, #6366f1, #8b5cf6);
            border: none;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: transform 0.15s, box-shadow 0.15s;
            flex-shrink: 0;
        }
        #agentic-chat-send:hover {
            transform: scale(1.08);
            box-shadow: 0 2px 12px rgba(99,102,241,0.4);
        }
        #agentic-chat-send svg { width: 18px; height: 18px; fill: white; }
    </style>

    <div id="agentic-chat-bubble">
        <svg viewBox="0 0 24 24"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm0 14H6l-2 2V4h16v12z"/></svg>
    </div>

    <div id="agentic-chat-panel">
        <div id="agentic-chat-header">
            <div class="dot"></div>
            <span>Agentic Browser</span>
        </div>
        <div class="agentic-chat-section">
            <button id="agentic-chat-toggle-messages" class="agentic-chat-toggle" type="button">Messages ▾</button>
            <div id="agentic-chat-messages-wrap" class="agentic-chat-section-body">
                <div id="agentic-chat-messages"></div>
            </div>
        </div>
        <div class="agentic-chat-section">
            <button id="agentic-chat-toggle-state" class="agentic-chat-toggle" type="button">State ▸</button>
            <div id="agentic-chat-state-wrap" class="agentic-chat-section-body">
                <pre id="agentic-chat-state"></pre>
            </div>
        </div>
        <div id="agentic-chat-input-area">
            <div id="agentic-chat-controls">
                <select id="agentic-chat-provider" class="agentic-chat-select">
                    <option value="google">Google</option>
                    <option value="groq">Groq</option>
                </select>
                <select id="agentic-chat-model" class="agentic-chat-select"></select>
            </div>
            <div id="agentic-chat-composer">
                <input id="agentic-chat-input" type="text" placeholder="Ask me anything..." autocomplete="off"/>
                <button id="agentic-chat-send">
                    <svg viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
                </button>
            </div>
            <div id="agentic-chat-footer">
                <div id="agentic-chat-loop-wrap">
                    <label for="agentic-chat-loop-limit">Loop limit</label>
                    <input id="agentic-chat-loop-limit" type="number" min="1" step="1" value="3"/>
                </div>
            </div>
        </div>
    </div>
    `;
    (document.body || document.documentElement).appendChild(root);

    // ── Logic ──
    const bubble = document.getElementById('agentic-chat-bubble');
    const panel = document.getElementById('agentic-chat-panel');
    const msgs = document.getElementById('agentic-chat-messages');
    const msgsWrap = document.getElementById('agentic-chat-messages-wrap');
    const stateWrap = document.getElementById('agentic-chat-state-wrap');
    const statePre = document.getElementById('agentic-chat-state');
    const toggleMessagesBtn = document.getElementById('agentic-chat-toggle-messages');
    const toggleStateBtn = document.getElementById('agentic-chat-toggle-state');
    const input = document.getElementById('agentic-chat-input');
    const loopLimitInput = document.getElementById('agentic-chat-loop-limit');
    const sendBtn = document.getElementById('agentic-chat-send');
    const providerSelect = document.getElementById('agentic-chat-provider');
    const modelSelect = document.getElementById('agentic-chat-model');

    const MODEL_OPTIONS = {
        groq: [
            'llama-3.1-8b-instant',
            'llama-3.3-70b-versatile',
            'openai/gpt-oss-120b',
            'openai/gpt-oss-20b',
        ],
        google: [
            'gemini-2.5-flash',
            'gemini-2.5-flash-lite',
            'gemini-2.0-flash',
        ],
    };

    let ws = null;
    let wsRetries = 0;
    let isPanelOpen = false;
    const PANEL_KEY = 'agentic-chat-panel-open';
    const LOOP_LIMIT_KEY = 'agentic-chat-loop-limit';
    const MSGS_DROPDOWN_KEY = 'agentic-chat-messages-open';
    const STATE_DROPDOWN_KEY = 'agentic-chat-state-open';
    let latestAgentState = {};
    let latestUiState = {};

    function getStored(key, fallback = '') {
        try {
            const value = window.localStorage.getItem(key);
            return value === null ? fallback : value;
        } catch (e) {
            return fallback;
        }
    }

    function setStored(key, value) {
        try {
            window.localStorage.setItem(key, value);
        } catch (e) {}
    }

    function setSectionOpen(section, open) {
        if (section === 'messages') {
            msgsWrap.style.display = open ? 'block' : 'none';
            toggleMessagesBtn.textContent = open ? 'Messages ▾' : 'Messages ▸';
            setStored(MSGS_DROPDOWN_KEY, open ? 'open' : 'closed');
            return;
        }
        if (section === 'state') {
            stateWrap.style.display = open ? 'block' : 'none';
            toggleStateBtn.textContent = open ? 'State ▾' : 'State ▸';
            setStored(STATE_DROPDOWN_KEY, open ? 'open' : 'closed');
        }
    }

    function renderState() {
        const statePayload = {
            ui_state: latestUiState,
            agent_state: latestAgentState,
        };
        statePre.textContent = JSON.stringify(statePayload, null, 2);
    }

    function populateModels(provider, selectedModel = '') {
        const models = MODEL_OPTIONS[provider] || [];
        modelSelect.innerHTML = '';
        models.forEach((model) => {
            const option = document.createElement('option');
            option.value = model;
            option.textContent = model;
            modelSelect.appendChild(option);
        });
        if (selectedModel && models.includes(selectedModel)) {
            modelSelect.value = selectedModel;
        }
    }

    const savedProvider = getStored('agentic-chat-provider', 'google');
    const savedModel = getStored('agentic-chat-model', 'gemini-2.5-flash-lite');
    const savedLoopLimit = parseInt(getStored(LOOP_LIMIT_KEY, '3'), 10);
    const savedPanelOpenRaw = getStored(PANEL_KEY, 'closed');
    const hasSavedPanelState = savedPanelOpenRaw === 'open' || savedPanelOpenRaw === 'closed';
    const savedPanelOpen = savedPanelOpenRaw === 'open';
    providerSelect.value = MODEL_OPTIONS[savedProvider] ? savedProvider : 'google';
    populateModels(providerSelect.value, savedModel);
    loopLimitInput.value = String(Number.isFinite(savedLoopLimit) && savedLoopLimit > 0 ? savedLoopLimit : 3);

    providerSelect.addEventListener('change', () => {
        populateModels(providerSelect.value);
        setStored('agentic-chat-provider', providerSelect.value);
        setStored('agentic-chat-model', modelSelect.value);
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({
                type: 'ui_state',
                panel_open: isPanelOpen,
                provider: providerSelect.value,
                model: modelSelect.value,
                loop_limit: parseInt(loopLimitInput.value || '3', 10) || 3,
            }));
        }
    });

    modelSelect.addEventListener('change', () => {
        setStored('agentic-chat-provider', providerSelect.value);
        setStored('agentic-chat-model', modelSelect.value);
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({
                type: 'ui_state',
                panel_open: isPanelOpen,
                provider: providerSelect.value,
                model: modelSelect.value,
                loop_limit: parseInt(loopLimitInput.value || '3', 10) || 3,
            }));
        }
    });
    loopLimitInput.addEventListener('change', () => {
        let value = parseInt(loopLimitInput.value || '3', 10);
        if (!Number.isFinite(value) || value < 1) value = 3;
        loopLimitInput.value = String(value);
        setStored(LOOP_LIMIT_KEY, String(value));
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({
                type: 'ui_state',
                panel_open: isPanelOpen,
                provider: providerSelect.value,
                model: modelSelect.value,
                loop_limit: value,
            }));
        }
    });

    function renderHistory(messages) {
        msgs.innerHTML = '';
        if (!Array.isArray(messages)) return;
        messages.forEach((m) => {
            const t = m && m.type ? m.type : 'agent';
            const c = m && typeof m.content === 'string' ? m.content : '';
            if (c) addMessage(c, t);
        });
    }

    function connectWS() {
        try {
            ws = new WebSocket('ws://localhost:8765');
            ws.onopen = () => {
                wsRetries = 0;
                ws.send(JSON.stringify({
                    type: 'ui_state',
                    panel_open: isPanelOpen,
                    provider: providerSelect.value,
                    model: modelSelect.value,
                    loop_limit: parseInt(loopLimitInput.value || '3', 10) || 3,
                }));
            };
            ws.onmessage = (evt) => {
                try {
                    const data = JSON.parse(evt.data);
                    if (data.type === 'history') {
                        renderHistory(data.messages);
                        return;
                    }
                    if (data.type === 'ui_state') {
                        latestUiState = {
                            panel_open: Boolean(data.panel_open),
                            provider: data.provider || providerSelect.value,
                            model: data.model || modelSelect.value,
                            loop_limit: data.loop_limit || parseInt(loopLimitInput.value || '3', 10) || 3,
                        };
                        renderState();
                        if (!hasSavedPanelState) {
                            setPanelOpen(Boolean(data.panel_open), false);
                        }
                        const provider = (data.provider || '').toLowerCase();
                        const model = data.model || '';
                        const loopLimit = parseInt(String(data.loop_limit || ''), 10);
                        if (!getStored('agentic-chat-provider', '') && MODEL_OPTIONS[provider]) {
                            providerSelect.value = provider;
                            populateModels(provider, model);
                            setStored('agentic-chat-provider', provider);
                            setStored('agentic-chat-model', modelSelect.value);
                        }
                        if (!getStored(LOOP_LIMIT_KEY, '') && Number.isFinite(loopLimit) && loopLimit >= 1) {
                            loopLimitInput.value = String(loopLimit);
                            setStored(LOOP_LIMIT_KEY, String(loopLimit));
                        }
                        return;
                    }
                    if (data.type === 'agent_state') {
                        latestAgentState = data;
                        renderState();
                        return;
                    }
                    addMessage(data.content, data.type || 'agent');
                } catch(e) {
                    addMessage(evt.data, 'agent');
                }
            };
            ws.onclose = () => {
                if (wsRetries < 10) {
                    setTimeout(() => { wsRetries++; connectWS(); }, 2000);
                }
            };
            ws.onerror = () => {};
        } catch(e) {}
    }

    function addMessage(text, type) {
        if (type === 'response') type = 'agent';

        const div = document.createElement('div');
        div.className = 'agentic-msg ' + type;
        div.textContent = text;
        msgs.appendChild(div);
        msgs.scrollTop = msgs.scrollHeight;
    }

    function sendMessage() {
        const text = input.value.trim();
        if (!text) return;
        input.value = '';
        const selectedProvider = providerSelect.value;
        const selectedModel = modelSelect.value;
        setStored('agentic-chat-provider', selectedProvider);
        setStored('agentic-chat-model', selectedModel);
        let selectedLoopLimit = parseInt(loopLimitInput.value || '3', 10);
        if (!Number.isFinite(selectedLoopLimit) || selectedLoopLimit < 1) {
            selectedLoopLimit = 3;
            loopLimitInput.value = '3';
        }
        setStored(LOOP_LIMIT_KEY, String(selectedLoopLimit));
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({
                content: text,
                provider: selectedProvider,
                model: selectedModel,
                loop_limit: selectedLoopLimit,
            }));
        } else {
            addMessage(text, 'user');
            addMessage('Not connected. Retrying...', 'system');
            connectWS();
        }
    }

    function setPanelOpen(open, sync = true) {
        isPanelOpen = open;
        setStored(PANEL_KEY, open ? 'open' : 'closed');
        panel.classList.toggle('open', open);
        panel.style.display = open ? 'flex' : 'none';
        if (sync && ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({
                type: 'ui_state',
                panel_open: open,
                provider: providerSelect.value,
                model: modelSelect.value,
                loop_limit: parseInt(loopLimitInput.value || '3', 10) || 3,
            }));
        }
        if (open && !ws) {
            connectWS();
        }
        if (open) {
            setTimeout(() => input.focus(), 100);
        }
    }

    bubble.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        setPanelOpen(!isPanelOpen, true);
    });

    sendBtn.addEventListener('click', sendMessage);
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') sendMessage();
    });
    toggleMessagesBtn.addEventListener('click', () => {
        setSectionOpen('messages', msgsWrap.style.display === 'none');
    });
    toggleStateBtn.addEventListener('click', () => {
        setSectionOpen('state', stateWrap.style.display === 'none');
    });

    setSectionOpen('messages', getStored(MSGS_DROPDOWN_KEY, 'open') === 'open');
    setSectionOpen('state', getStored(STATE_DROPDOWN_KEY, 'closed') === 'open');
    renderState();
    setPanelOpen(savedPanelOpen, false);
    connectWS();
})();
"""


class BrowserManager:
    def __init__(self):
        self.playwright = None
        self.browser: Optional[PlaywrightBrowser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.h2t = html2text.HTML2Text()
        self.h2t.ignore_links = False
        self.h2t.ignore_images = True
        self.h2t.ignore_emphasis = True
        self._chat_injected = False

    async def start(self):
        """Initializes the browser session."""
        if not self.playwright:
            self.playwright = await async_playwright().start()
            # Launch maximized to cover the full screen area.
            self.browser = await self.playwright.chromium.launch(
                headless=False,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--start-maximized",
                ],
            )
            self.context = await self.browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                no_viewport=True,
            )
            self.page = await self.context.new_page()

            # Auto-inject chat widget for all tabs and navigations.
            await self._setup_chat_injection()

    async def _setup_chat_injection(self):
        """Inject chat widget on all pages/tabs in this browser context."""
        if not self.context:
            return

        # Runs on every new document for every tab in this context.
        await self.context.add_init_script(CHAT_WIDGET_JS)

        async def _inject_once(page: Page):
            try:
                await page.evaluate(CHAT_WIDGET_JS)
                self._chat_injected = True
            except Exception:
                pass

        # Existing open tabs
        for p in self.context.pages:
            p.on("domcontentloaded", lambda pg=p: asyncio.ensure_future(_inject_once(pg)))
            await _inject_once(p)

        # Future tabs opened by user/actions
        def _on_new_page(page: Page):
            page.on("domcontentloaded", lambda: asyncio.ensure_future(_inject_once(page)))
            asyncio.ensure_future(_inject_once(page))

        self.context.on("page", _on_new_page)

    async def inject_chat_widget(self):
        """Manually inject the chat widget into the current page."""
        if self.page:
            try:
                await self.page.evaluate(CHAT_WIDGET_JS)
                self._chat_injected = True
            except Exception as e:
                print(f"⚠️ Failed to inject chat widget: {e}")

    async def stop(self):
        async def _safe_close(label: str, closer):
            try:
                await closer()
            except Exception as e:
                msg = str(e).lower()
                if "target page, context or browser has been closed" not in msg:
                    print(f"⚠️ {label} close warning: {e}")

        if self.page:
            await _safe_close("page", self.page.close)
            self.page = None
        if self.context:
            await _safe_close("context", self.context.close)
            self.context = None
        if self.browser:
            await _safe_close("browser", self.browser.close)
            self.browser = None
        if self.playwright:
            await _safe_close("playwright", self.playwright.stop)
            self.playwright = None

    async def navigate(self, url: str) -> str:
        if not self.page:
            await self.start()
        try:
            if not url.startswith('http'):
                url = f'https://{url}'
            await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            return f"Navigated to {url}"
        except Exception as e:
            return f"Error navigating to {url}: {str(e)}"

    async def click(self, selector: str) -> str:
        if not self.page:
            return "Error: No active page"
        try:
            # Try to be smart about selectors. If it looks like text, use text selector
            if not (selector.startswith('.') or selector.startswith('#') or selector.startswith('//')):
                 # It might be text
                 await self.page.click(f"text={selector}", timeout=5000)
            else:
                await self.page.click(selector, timeout=5000)
            return f"Clicked '{selector}'"
        except Exception as e:
            return f"Error clicking '{selector}': {str(e)}"

    async def type_text(self, selector: str, text: str) -> str:
        if not self.page:
            return "Error: No active page"
        try:
            await self.page.fill(selector, text, timeout=5000)
            return f"Typed '{text}' into '{selector}'"
        except Exception as e:
            return f"Error typing into '{selector}': {str(e)}"

    async def get_content(self) -> str:
        if not self.page:
            return "Error: No active page"
        try:
            content = await self.page.content()
            # Convert to nicely formatted markdown to save tokens and help LLM
            markdown = self.h2t.handle(content)
            # Truncate if too huge
            if len(markdown) > 8000:
                markdown = markdown[:8000] + "\n...[Content Truncated]"
            return markdown
        except Exception as e:
            return f"Error getting content: {str(e)}"

    async def google_search(self, query: str) -> str:
        # Import lazy to avoid circular dependency
        from tools.search.google import google_search_safe
        
        if not self.page:
            await self.start()
        try:
            await google_search_safe(query)
            return f"Searched Google for: {query}"
        except Exception as e:
            return f"Error searching: {str(e)}"
            
    async def get_interactive_elements(self) -> str:
        """Returns a list of potential interactive elements to help the agent."""
        if not self.page:
             return "Error: No active page"
        try:
            # Simple heuristic to find buttons and links
            elements = await self.page.evaluate("""() => {
                const els = Array.from(document.querySelectorAll('a, button, input, [role="button"]'));
                return els.map(e => {
                    // Skip our chat widget elements
                    if (e.closest('#agentic-chat-root')) return null;
                    let text = e.innerText || e.value || e.getAttribute('aria-label') || '';
                    text = text.trim().substring(0, 30);
                    let selector = '';
                    if (e.id) selector = '#' + e.id;
                    else if (e.className && typeof e.className === 'string') selector = '.' + e.className.split(' ').join('.');
                    return {tag: e.tagName, text: text, selector: selector};
                }).filter(e => e && e.text.length > 0).slice(0, 50);
            }""")
            
            summary = "Interactive Elements:\n"
            for el in elements:
                summary += f"- {el['tag']}: [{el['text']}] (Selector: {el['selector']})\n"
            return summary
        except Exception as e:
            return f"Error getting interactive elements: {str(e)}"

    async def scroll(self, amount: int = 500) -> str:
        if not self.page:
            return "Error: No active page"
        try:
            await self.page.evaluate(f"window.scrollBy(0, {amount})")
            return f"Scrolled down by {amount} pixels"
        except Exception as e:
            return f"Error scrolling: {str(e)}"

# Global instance
browser_manager = BrowserManager()
