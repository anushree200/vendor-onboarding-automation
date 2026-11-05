import os
import traceback
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types


def main() -> int:
    load_dotenv(dotenv_path=Path(__file__).with_name(".env"))
    api_key = os.getenv("LLM_API_KEY")
    if not api_key:
        print(
            "RuntimeError: LLM_API_KEY is not set in the project .env file. "
            "No Gemini request was made."
        )
        return 1

    provider = (os.getenv("LLM_PROVIDER") or "gemini").strip().casefold()
    if provider != "gemini":
        print(
            f"RuntimeError: LLM_PROVIDER is {provider!r}, not 'gemini'. "
            "No Gemini request was made."
        )
        return 1

    model = os.getenv("LLM_MODEL") or "gemini-2.0-flash"
    try:
        client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=15_000),
        )
        response = client.models.generate_content(
            model=model,
            contents="Reply with exactly: Gemini API connection successful.",
        )
        if not response.text:
            raise RuntimeError("Gemini returned an empty text response.")
        print(response.text)
        return 0
    except Exception as error:
        details = traceback.format_exc().replace(api_key, "[REDACTED]")
        print(f"Gemini request failed ({type(error).__name__}):\n{details}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
