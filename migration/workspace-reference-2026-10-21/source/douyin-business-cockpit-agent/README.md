# Douyin Business Cockpit Agent

抖音业务智能分析总控Agent — a LangGraph multi-agent orchestrator that coordinates
two existing Databricks Genie Agents for unified Chinese-language business analysis.

## Architecture

```
User (Chinese question)
    │
    ▼
┌─────────────────────────────────────┐
│  LangGraph Agent (DurableAgentServer) │
│  System prompt: routing + guardrails │
│  Model: Claude Sonnet 4.5 (AI Gateway)│
└──────────┬──────────────┬───────────┘
           │              │
     ┌─────▼─────┐  ┌────▼──────────┐
     │ A1 Genie   │  │ A2 Genie       │
     │ Sales &    │  │ Competition &  │
     │ Operations │  │ Market Analysis│
     │ (L1–L4)    │  │ (L1/L2 only)   │
     └───────────┘  └───────────────┘
```

- **Framework**: LangGraph + DurableAgentServer (Agent Bricks CLI v0.5.0)
- **Model**: `system.ai.claude-sonnet-4-5` via Unity Catalog AI Gateway
- **Tools**: Two Genie Agent tools bound via `agent.toml`
- **Tracing**: MLflow traces in Unity Catalog
- **Memory/Session**: Managed by Agent Bricks

## Connected Genie Agents

| Tool Name | Space ID | Scope |
|-----------|----------|-------|
| a1_sales_operations | 01f1c60941551d9b94f73515f09e4b7b | Internal sales, GMV, refunds, targets, livestream, ads |
| a2_competition_market | 01f1c60944fa1a43bf79ff847cd5323d | Market share, rankings, 5A assets, competitor analysis |

## Project Structure

```
douyin-business-cockpit-agent/
├── agent/
│   ├── agent.py          # Main LangGraph agent with routing prompt
│   ├── prompts.py        # System prompt (routing rules + guardrails)
│   ├── tools/            # Local Python tools
│   └── mcps.py           # MCP server config
├── runtime/
│   ├── main.py           # DurableAgentServer entrypoint
│   ├── adapter.py        # Invoke/recover adapters
│   └── ui.py             # Chat UI
├── tests/
│   └── test_routing.py   # 7 test cases (unit + integration)
├── agent.toml            # Declarative tool bindings
├── pyproject.toml        # Dependencies
└── .env                  # Databricks auth config
```

## Quick Start

```bash
# 1. Set up auth
cp .env.example .env
# Edit .env: set DATABRICKS_CONFIG_PROFILE or DATABRICKS_HOST+TOKEN

# 2. Run locally
agentbricks dev
# Open http://localhost:8000

# 3. Run unit tests
python -m pytest tests/test_routing.py -v -k "not integration"

# 4. Run integration tests (requires live Genie access)
python -m pytest tests/test_routing.py -v -k "integration"

# 5. Deploy
agentbricks deploy
```

## Business Guardrails

- Responds in Simplified Chinese by default
- All data is synthetic mock data — never presented as real performance
- Internal GMV ≠ External market GMV (never aggregated interchangeably)
- Official monthly targets unavailable; annual/12 is a hypothesis only
- A2 limited to L1/L2 descriptive queries (no causal analysis or strategy)
- Missing records are not treated as zero
- Data coverage: January–June 2025

## License

Internal use only.
