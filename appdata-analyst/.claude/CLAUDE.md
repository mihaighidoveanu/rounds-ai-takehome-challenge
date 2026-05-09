# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

See the root `../.claude/CLAUDE.md` for monorepo-wide architecture, commands, and a comparison of all implementations.

## Pydantic AI Specifics

**Agent (`agent/agent.py`)** is a Pydantic AI `Agent` with `deps_type=AgentDeps`. The model is **not** set on the agent (to avoid import-time client creation); instead `get_model()` selects the provider at runtime and is passed at each `run_sync()` call site. Tools are passed via the `tools=[]` constructor parameter (not decorators) so each tool lives in its own file under `agent/tools/`.

**Model selection** uses a `_PROVIDERS` list of `(API_KEY_var, MODEL_var)` tuples. The first provider whose API key env var is set wins. Current priority: **Groq → Anthropic → OpenAI**. To change priority, reorder `_PROVIDERS`. Each provider requires both its `*_API_KEY` and its `*_MODEL` env var (e.g. `GROQ_API_KEY` + `GROQ_MODEL=groq:openai/gpt-oss-120b`); missing either raises a `RuntimeError` at startup.

**Conversation history** stores `list[ModelMessage]` from Pydantic AI and is passed directly as `message_history=` to `run_sync()`.

**Feedback blocks** use the native `FeedbackButtonsElement` from `slack_sdk.models.blocks`. A single `feedback` action ID is registered.