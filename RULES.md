# DEVELOPMENT & MAINTENANCE RULES: OMP TELEGRAM BOT

## 1. Documentation Synchronization Rule (Mandatory)
Every time a new feature, capability, command, configuration change, or architectural rule is added, changed, or removed in this bot:

1. **`README.md` MUST be updated**:
   - Reflect user-facing changes (new commands, usage instructions, dependencies).
   - Document any changes in environment variables or service control.

2. **`AGENTS.md` MUST be updated**:
   - Reflect agent execution flow, tool bindings, command mappings, and security/state logic.
   - Update architecture diagrams or state definitions if session handling changes.

3. **`PRD.md` MUST be updated**:
   - Keep requirements, acceptance criteria, invariants, and scope up to date.
   - Document new functional and non-functional specifications.

## 2. Invariant Rules
- **No Stale Docs**: Code changes and documentation updates must be committed or applied in the same turn/milestone.
- **Single Bot Instance**: Only one polling listener process must run per bot token at any time to prevent Telegram 409 Conflict.
- **Access Control**: Every command and prompt handler must enforce `is_authorized(user_id)`.
- **Clean Output**: Terminal control codes and ANSI escapes must always be sanitized before sending to Telegram.
- **Fail-Safe Subprocess**: Long-running processes must be interruptible via `/stop`, killing all children in the process group without triggering fallback loops.
- **Secret Isolation**: Never commit tokens, user IDs, or environment-specific private paths. Keep all secrets in `.env` (gitignored) and maintain `.env.example`.
