from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv

from .logger import logger

load_dotenv()

# Prompt template for code summarization
_SUMMARY_PROMPT = (
    "You are documenting code for search/navigation. Think through the code, then respond "
    "as 5-8 concise bullet points covering:\n"
    "- Purpose (one line)\n"
    "- Inputs/outputs\n"
    "- Side effects (DB/HTTP/files)\n"
    "- Exceptions or logging\n"
    "- Concurrency/latency considerations\n"
    "- Key callees/callers\n"
    "Rules: use plain-text bullets (no code fences), obey semantic Markdown (bullets only), "
    "≤ 80 words total, and prefer confident statements over speculation."
)


async def summarize_many_async(
    texts: list[str], *, model: str, concurrency: int = 10
) -> list[str | None]:
    """
    Concurrent async batch summarization using AsyncOpenAI chat.completions API.

    Args:
        texts: Code snippets to summarize
        model: OpenAI model to use (e.g., 'gpt-5-nano-2025-08-07', 'gpt-4o-mini', 'gpt-4o')
        concurrency: Max parallel requests (limited by semaphore)

    Returns:
        List of summaries (None on failure) preserving input order

    Environment variables:
        CODEINDEX_SUMMARY_TIMEOUT: Request timeout in seconds (default: 30)
        CODEINDEX_SUMMARY_RETRIES: Max retry attempts (default: 2)

    Note:
        - Uses max_completion_tokens instead of deprecated max_tokens parameter
        - Temperature (0.3) is only set for non-nano models, as nano models don't support it
        - Recommended: gpt-5-nano-2025-08-07 (fastest), gpt-4o-mini (balanced), gpt-4o (quality)
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY is not set")

    default_timeout = float(os.getenv("CODEINDEX_SUMMARY_TIMEOUT", "30"))
    max_retries = int(os.getenv("CODEINDEX_SUMMARY_RETRIES", "2"))

    from openai import AsyncOpenAI

    sem = asyncio.Semaphore(max(1, concurrency))

    async with AsyncOpenAI(
        api_key=api_key,
        timeout=default_timeout,
        max_retries=max_retries,
    ) as client:

        async def _one(i: int, t: str):
            async with sem:
                try:
                    logger.debug(
                        "Summarizing snippet %d/%d: model=%s, timeout=%.1fs, text_len=%d",
                        i + 1,
                        len(texts),
                        model,
                        default_timeout,
                        len(t or ""),
                    )

                    # Build API call parameters
                    is_reasoning_model = "gpt-5" in model.lower()
                    params = {
                        "model": model,
                        "messages": [
                            {
                                "role": "system",
                                "content": (
                                    "You are a concise code documenter for search indexing. "
                                    "Reason internally, then respond exactly as instructed."
                                ),
                            },
                            {
                                "role": "user",
                                "content": _SUMMARY_PROMPT
                                + "\n\n--- CODE/CONTEXT ---\n"
                                + (t or "")[:4000],
                            },
                        ],
                        "max_completion_tokens": 1000
                        if "nano" in model.lower() or "o1" in model.lower()
                        else 200,
                    }

                    if is_reasoning_model:
                        params["reasoning_effort"] = os.getenv(
                            "CODEINDEX_SUMMARY_REASONING", "minimal"
                        )
                    elif "nano" not in model.lower() and "o1" not in model.lower():
                        params["temperature"] = 0.3

                    resp = await client.chat.completions.create(**params)

                    content = resp.choices[0].message.content
                    if content:
                        logger.debug(
                            "Summary %d: succeeded (%d chars)", i + 1, len(content)
                        )
                        return i, content.strip()
                    else:
                        logger.warning("Summary %d: empty response", i + 1)
                        return i, None

                except Exception as e:
                    logger.warning(
                        "Summary %d: failed after %d retries: %s: %s",
                        i + 1,
                        max_retries,
                        type(e).__name__,
                        str(e)[:100],  # Truncate long error messages
                    )
                    return i, None

        tasks = [asyncio.create_task(_one(i, txt)) for i, txt in enumerate(texts)]
        results: list[str | None] = [None] * len(texts)
        for fut in asyncio.as_completed(tasks):
            i, val = await fut
            results[i] = val
        return results
