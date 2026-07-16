# Memex — Open Agent Memory Layer

**Free, persistent memory for any AI agent.**
Works with Claude, ChatGPT, Perplexity, and any custom agent via MCP or REST.

## Quick Start

### From Claude.ai (MCP)
```json
{
  "mcpServers": {
    "memex": { "url": "https://memex.gummi.lt/mcp" }
  }
}
```

### From any agent (REST)
```bash
# Store a memory
curl -X POST https://memex.gummi.lt/v1/memory \
  -H "Content-Type: application/json" \
  -d '{"namespace": "my-agent", "content": "User prefers dark mode"}'

# Search memories
curl -X POST https://memex.gummi.lt/v1/memory/search \
  -H "Content-Type: application/json" \
  -d '{"namespace": "my-agent", "query": "user preferences"}'
```

### From ChatGPT (Custom Action)
Import `openapi_chatgpt.json` as a Custom Action in your GPT configuration.

## Architecture

```
Phase 1 (now):    Centralized FastAPI + SQLite, semantic search, MCP server
Phase 2 (Q4 26):  IPFS/Arweave storage — memories become portable
Phase 3 (Q1 27):  Wallet-based identity — you own your namespace
Phase 4 (Q2 27):  On-chain registry — discover and trade knowledge namespaces
```

## Self-Healing

A background monitor checks API health, search quality, and DB size every 60s.
Low-importance memories are pruned automatically when storage approaches limits.

## License
MIT — free for any use, commercial or personal.

