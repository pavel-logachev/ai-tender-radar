"""Agent loop over an OpenAI-compatible chat API (OpenRouter).

Code enforces only hard budgets (turns, money). Behaviour is steered by the prompt.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import httpx

from agent_radar.lead_agent import tools as T

DEFAULT_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "qwen/qwen3.7-flash"
FALLBACK_MODEL = "deepseek/deepseek-v4.1-flash"
PROMPT_PATH = Path(__file__).with_name("prompt.md")
TOOL_OUTPUT_CAP = 16000
_NUDGE = ("Бюджет шагов почти исчерпан. Больше не вызывай инструменты: выдай итоговый JSON "
          "по схеме прямо сейчас с тем, что уже известно, честно отметив пробелы.")


@dataclass
class AgentRun:
    tender_id: str
    model: str
    final_text: str | None
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    tool_calls: int = 0
    seconds: float = 0.0
    error: str | None = None
    tool_log: list[dict] = field(default_factory=list)
    tool_text: str = ""  # everything the agent actually read, for grounding checks


def load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def extract_json(text: str | None) -> dict | None:
    if not text or "{" not in text:
        return None
    try:
        value = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def run_agent(row: dict, *, api_key: str, model: str = DEFAULT_MODEL, system: str | None = None,
              max_turns: int = 30, max_cost_usd: float = 0.25, url: str = DEFAULT_URL,
              transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep,
              tool_impl: dict | None = None, proxy: str | None = None) -> AgentRun:
    tender_id = row["card"]["id"]
    procedure = T.TenderTools(row)
    functions = {"read_procedure": procedure.read_procedure, "read_document": procedure.read_document,
                 "web_search": T.web_search, "fetch_page": T.fetch_page, "fns_reports": T.fns_reports}
    functions.update(tool_impl or {})
    messages = [{"role": "system", "content": system or load_prompt()},
                {"role": "user", "content": "Проведи исследование по закупке и подготовь лид. Начни с read_procedure."}]
    run = AgentRun(tender_id=tender_id, model=model, final_text=None)
    started = time.monotonic()
    reads: list[str] = []
    # OpenRouter rejects Russian datacenter addresses: model calls may need a foreign exit (web tools stay direct).
    proxy = proxy or os.environ.get("OPENROUTER_PROXY") or None
    with httpx.Client(timeout=300, transport=transport, proxy=None if transport else proxy) as client:
        for turn in range(max_turns):
            last = turn == max_turns - 1
            if turn == max_turns - 2 or run.cost_usd >= max_cost_usd:
                messages.append({"role": "user", "content": _NUDGE})
            body = {"model": model, "messages": messages, "max_tokens": 8000}
            if not last and run.cost_usd < max_cost_usd:
                body["tools"] = T.SCHEMAS
            else:
                body["tool_choice"] = "none"
                body["tools"] = T.SCHEMAS
            data: dict = {}
            for attempt in range(5):
                try:
                    response = client.post(url, headers={"Authorization": f"Bearer {api_key}"}, json=body)
                    data = response.json()
                    if response.status_code == 200 and "choices" in data:
                        break
                except (httpx.HTTPError, ValueError) as error:
                    data = {"error": str(error)}
                sleep(3 * (attempt + 1))
            if "choices" not in data:
                run.error = json.dumps(data.get("error", data), ensure_ascii=False)[:300]
                break
            usage = data.get("usage") or {}
            run.cost_usd += float(usage.get("cost") or 0)
            run.tokens_in += int(usage.get("prompt_tokens") or 0)
            run.tokens_out += int(usage.get("completion_tokens") or 0)
            message = data["choices"][0]["message"]
            messages.append({key: value for key, value in message.items()
                             if key in ("role", "content", "tool_calls") and value is not None})
            calls = message.get("tool_calls") or []
            if not calls:
                run.final_text = message.get("content")
                break
            for call in calls:
                name = call["function"]["name"]
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                    output = functions[name](**args) if name in functions else f"unknown tool {name}"
                except (ValueError, TypeError, KeyError) as error:
                    args, output = {}, f"tool error: {error}"
                except Exception as error:  # a broken tool must not kill the whole run
                    args, output = {}, f"tool error: {error}"
                output = str(output)[:TOOL_OUTPUT_CAP]
                run.tool_calls += 1
                run.tool_log.append({"turn": turn, "tool": name, "args": args, "chars": len(output)})
                reads.append(output)
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": output})
    run.seconds = round(time.monotonic() - started, 1)
    run.cost_usd = round(run.cost_usd, 5)
    run.tool_text = "\n".join(reads)
    return run
