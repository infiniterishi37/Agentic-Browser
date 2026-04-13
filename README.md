# Agentic Browser
A web browser agent powered by LangGraph, Google Gemini, and Playwright.

## Setup
1. **Prerequisites**: Python 3.10+
2. **Install**:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   playwright install
   ```
3. **API Key**:
   - Get a key from [Google AI Studio](https://aistudio.google.com/app/apikey)
   - Copy `.env.example` to `.env`
   - Paste your key in `.env`

## Usage
Run the agent:
```bash
python main.py
```
Enter your instructions, e.g., "Go to google.com and search for Agentic AI".
