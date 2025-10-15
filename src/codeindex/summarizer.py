from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def summarize_code(text: str, *, model: str = "gpt-5-nano") -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY is not set")
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key)
        prompt = (
            "You are documenting code for search/navigation. Summarize BRIEFLY as 5-8 bullets: "
            "- Purpose (1 line)\\n- Inputs/outputs\\n- Side effects/state or I/O (DB/HTTP/files)\\n"
            "- Exceptions or logging\\n- Concurrency/latency notes\\n- Key callees/callers\\n"
            "Respond in plain text bullets; no code fences; <= 80 words."
        )
        resp = client.responses.create(
            model=model, input=prompt + "\\n\\n--- CODE/CONTEXT ---\\n" + text[:4000]
        )
        out = getattr(resp, "output_text", None) or getattr(resp, "content", None)
        if isinstance(out, str):
            return out.strip()
        if hasattr(resp, "output"):
            chunks = []
            for item in resp.output:
                if isinstance(item, dict) and item.get("type") == "output_text":
                    chunks.append(item["text"])
            if chunks:
                return "\\n".join(chunks).strip()
        raise ValueError("No output from OpenAI")
    except Exception:
        raise ValueError("Error summarizing code")
