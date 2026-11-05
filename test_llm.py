import traceback
from itertools import islice
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

from config import get_setting
from llm import _PROVIDER_MODELS


def main() -> int:
    load_dotenv(dotenv_path=Path(__file__).with_name(".env"))
    enabled = (get_setting("LLM_ENABLED", "true") or "true").strip().casefold()
    if enabled in {"false", "0", "no", "off"}:
        print("LLM_ENABLED is false. No Gemini request was made.")
        return 1

    api_key = get_setting("LLM_API_KEY")
    if not api_key:
        print(
            "RuntimeError: LLM_API_KEY is not set in the project .env file. "
            "No Gemini request was made."
        )
        return 1

    provider = (get_setting("LLM_PROVIDER", "gemini") or "gemini").strip().casefold()
    if provider != "gemini":
        print(
            f"RuntimeError: LLM_PROVIDER is {provider!r}, not 'gemini'. "
            "No Gemini request was made."
        )
        return 1

    model = get_setting("LLM_MODEL", _PROVIDER_MODELS["gemini"])
    try:
        client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                timeout=10_000,
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )
        print("Gemini models supporting generateContent (first 15):")
        try:
            supported_models = (
                model_info
                for model_info in client.models.list()
                if "generatecontent"
                in {
                    str(action).replace("_", "").casefold()
                    for action in (model_info.supported_actions or [])
                }
            )
            model_names = [
                model_info.name
                for model_info in islice(supported_models, 15)
                if model_info.name
            ]
            for name in model_names:
                print(f"- {name}")
            if not model_names:
                print("(No matching models were returned.)")
        except Exception as error:
            details = traceback.format_exc().replace(api_key, "[REDACTED]")
            print(
                f"Model listing failed ({type(error).__name__}):\n{details}"
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
