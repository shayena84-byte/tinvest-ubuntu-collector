"""Replaceable decision sources. Only OpenAIProvider makes billable requests."""
import json
from pathlib import Path
import model_budget as budget
import trade_ledger as ledger
import v19_guard as guard


class OpenAIProvider:
    name = "openai"

    def decide(self, key, model, context):
        import ai_sandbox_agent as ai
        if guard.is_blocked():
            raise budget.BudgetBlocked("openai_quota_circuit_open")
        cfg = budget.config()
        safe_context = ledger.clean(context)
        size = len((ai.SYSTEM+ledger.encoded(safe_context)+json.dumps(ai.RESPONSE_SCHEMA)).encode("utf-8"))+1024
        call_id = budget.reserve(model, context.get("_mode", "run"), size,
                                 cycle_id=context.get("_cycle_id"), cfg=cfg)
        try:
            decision, usage = ai._request_openai(key, model, safe_context,
                                                max_output_tokens=cfg["max_output_tokens"])
        except Exception as exc:
            budget.complete(call_id, model, error_code=type(exc).__name__, cfg=cfg)
            if guard.is_quota_error(str(exc)):
                guard.block_quota()
            raise
        budget.complete(call_id, model, usage, cfg=cfg)
        return decision, usage


class OllamaProvider:
    name = "ollama"

    def decide(self, model, context):
        import ai_sandbox_agent as ai
        import ollama_sandbox_agent as local
        body = {"model": model, "stream": False, "think": False,
                "format": ai.RESPONSE_SCHEMA,
                "messages": [{"role": "system", "content": ai.SYSTEM},
                             {"role": "user", "content": ledger.encoded(context)}],
                "options": {"temperature": 0.3, "num_ctx": 8192, "num_predict": 700}}
        return local.parse_ollama(local.ollama_request("/api/chat", body, timeout=160))


class FixtureProvider:
    """Deterministic offline substitute, deliberately has no HTTP dependency."""
    name = "fixture"

    def __init__(self, path):
        self.path = Path(path)

    def decide(self, context=None):
        import ai_sandbox_agent as ai
        value = json.loads(self.path.read_text("utf-8"))
        return ai.decode_response({"status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": json.dumps(value)}]}]})


def main():
    import argparse
    from datetime import datetime, timezone
    import risk_policy
    p = argparse.ArgumentParser(description="Offline decision validation; no model or broker calls")
    p.add_argument("--fixture", type=Path, required=True)
    p.add_argument("--context", type=Path, required=True)
    args = p.parse_args()
    context = json.loads(args.context.read_text("utf-8"))
    decision = FixtureProvider(args.fixture).decide(context)
    trade, reason = risk_policy.validate(decision, context, {}, {}, datetime.now(timezone.utc))
    print(json.dumps({"decision": decision, "validation": reason or "approved",
                      "trade": trade, "network_requests": 0, "orders_submitted": 0}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
