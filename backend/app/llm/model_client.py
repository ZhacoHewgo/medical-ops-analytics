import asyncio
import json
import os

import httpx

_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
_DEPARTMENT_PLACEHOLDER = "科室=DEPT"


async def _completion(url, model, key, timeout, messages):
    if (url.scheme not in {"http", "https"} or not url.host or url.userinfo or url.query
            or url.fragment or (url.scheme == "http" and url.host not in _LOOPBACK)):
        raise ValueError("Invalid model endpoint")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    payload = {
        "model": model, "messages": messages, "temperature": 0, "max_tokens": 256,
        "response_format": {"type": "json_object"},
    }
    async with asyncio.timeout(timeout), httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, trust_env=False,
    ) as client:
        async with client.stream("POST", str(url).rstrip("/") + "/chat/completions",
                                 headers=headers, json=payload) as response:
            response.raise_for_status()
            content = bytearray()
            async for chunk in response.aiter_bytes():
                if len(content) + len(chunk) > 65536:
                    raise ValueError("Model response is too large")
                content.extend(chunk)
    message = json.loads(content)["choices"][0]["message"]
    return json.loads(message["content"])


def configured_generator():
    base_url = os.environ.get("LLM_API_BASE_URL", "").strip()
    model = os.environ.get("LLM_MODEL", "").strip()
    if not base_url and not model:
        return None
    key = os.environ.get("LLM_API_KEY", "").strip()

    def generate(question, messages):
        timeout = float(os.environ.get("LLM_TIMEOUT_SECONDS", "8"))
        if not (base_url and model and 0 < timeout <= 30):
            raise ValueError("Invalid model configuration")
        url = httpx.URL(base_url)
        department = None
        if url.host not in _LOOPBACK:
            from app.llm.nl2sql import candidate_prompt, parse_intent

            intent = parse_intent(question)
            scope = intent.scope or "全院"
            if scope.startswith("科室"):
                department, scope = scope, _DEPARTMENT_PLACEHOLDER
            grain = "每月" if intent.series else ""
            growth = "环比" if intent.growth else ""
            messages = candidate_prompt(
                f"{intent.first} 至 {intent.last} {scope} {grain}{intent.code}{growth}",
            )
        candidate = asyncio.run(_completion(url, model, key, timeout, messages))
        if department and isinstance(candidate, dict) and candidate.get("scope") == _DEPARTMENT_PLACEHOLDER:
            candidate["scope"] = department
        return candidate

    return generate
