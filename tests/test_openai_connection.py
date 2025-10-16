#!/usr/bin/env python3
"""
Test script to verify OpenAI API connectivity and model availability.
Run this before using the summarizer feature to validate your setup.

Usage:
    export OPENAI_API_KEY="your-api-key-here"
    uv run python test_openai_connection.py [model-name]

Example:
    uv run python test_openai_connection.py gpt-4o-mini
"""

import asyncio
import os
import sys

try:
    import pytest
except ImportError:  # pragma: no cover - pytest not required for manual script usage
    pytest = None

if pytest is not None:  # pragma: no cover - executed only in test environments
    pytestmark = pytest.mark.skip(
        reason="Manual connectivity check; excluded from automated test suite."
    )


async def test_openai_connection(model: str = "gpt-4o-mini"):
    """Test async OpenAI connection with timeout and retry settings."""
    api_key = os.getenv("OPENAI_API_KEY")

    if not api_key:
        print("❌ OPENAI_API_KEY environment variable is not set.")
        print("\nSet it with:")
        print('  export OPENAI_API_KEY="your-key-here"')
        return False

    print(f"✓ OPENAI_API_KEY is set (length: {len(api_key)})")
    print(f"✓ Testing model: {model}")

    # Use same settings as the summarizer
    timeout = float(os.getenv("CODEINDEX_SUMMARY_TIMEOUT", "30"))
    max_retries = int(os.getenv("CODEINDEX_SUMMARY_RETRIES", "2"))

    print(f"✓ Timeout: {timeout}s, Max retries: {max_retries}")

    try:
        from openai import AsyncOpenAI

        print("\n⏳ Testing OpenAI API connection...")

        async with AsyncOpenAI(
            api_key=api_key,
            timeout=timeout,
            max_retries=max_retries,
        ) as client:
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "You are a helpful assistant."},
                    {
                        "role": "user",
                        "content": "Say 'ok' if you receive this message.",
                    },
                ],
                max_completion_tokens=10,
            )

            content = resp.choices[0].message.content or ""
            print(f"✅ Success! Response: {content.strip()}")
            print(f"✓ Model: {resp.model}")
            print(f"✓ Usage: {resp.usage.total_tokens} tokens")
            return True

    except Exception as e:
        print(f"\n❌ Connection failed: {type(e).__name__}")
        print(f"   {str(e)[:200]}")

        # Provide helpful error messages
        error_str = str(e).lower()
        if "authentication" in error_str or "401" in error_str:
            print("\n💡 This looks like an authentication error.")
            print("   - Verify your API key is correct")
            print("   - Check if the key has expired")
        elif "model" in error_str or "404" in error_str:
            print(f"\n💡 Model '{model}' may not be available.")
            print("   Common models:")
            print("   - gpt-4o-mini (fast, cheap, recommended for summaries)")
            print("   - gpt-4o (high quality)")
            print("   - gpt-3.5-turbo (legacy, cheap)")
        elif "timeout" in error_str or "timed out" in error_str:
            print(f"\n💡 Request timed out after {timeout}s.")
            print("   Try increasing CODEINDEX_SUMMARY_TIMEOUT")
        elif "rate" in error_str or "429" in error_str:
            print("\n💡 Rate limit exceeded.")
            print("   - Wait a moment and try again")
            print("   - Check your OpenAI usage limits")

        return False


async def main():
    model = sys.argv[1] if len(sys.argv) > 1 else "gpt-4o-mini"

    print("=" * 70)
    print("OpenAI Connection Test")
    print("=" * 70)

    success = await test_openai_connection(model)

    print("\n" + "=" * 70)
    if success:
        print("✅ All checks passed! You're ready to use --summarizer")
        print("\nExample build command:")
        print(f"  uv run codeindex build src/ --out ./index --summarizer {model}")
        sys.exit(0)
    else:
        print("❌ Setup incomplete. Fix the issues above and try again.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
