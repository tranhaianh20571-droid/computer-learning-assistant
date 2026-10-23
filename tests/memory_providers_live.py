"""Explicit opt-in synthetic probes; output contains counts/status, never secrets."""
import argparse
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live:
        parser.error("--live required")
    from dotenv import dotenv_values
    import httpx
    config = dotenv_values(Path(__file__).resolve().parents[1] / "server" / ".env.memory")
    probes = [
        ("model", config["HINDSIGHT_API_LLM_BASE_URL"] + "/models", config["HINDSIGHT_API_LLM_API_KEY"], None),
        ("embedding", config["HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL"] + "/embeddings",
         config["HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY"],
         {"model": config["HINDSIGHT_API_EMBEDDINGS_OPENAI_MODEL"],
          "input": ["synthetic memory probe"], "dimensions": int(config["HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS"])}),
        ("rerank", config["HINDSIGHT_API_RERANKER_TYPESAFE_BASE_URL"] + "/v1/systemone",
         config["HINDSIGHT_API_RERANKER_TYPESAFE_API_KEY"],
         {"model": config["HINDSIGHT_API_RERANKER_TYPESAFE_MODEL"],
          "state": "Question: user prefers examples",
          "questions": {"rank": {"type": "choice", "instructions": "Choose the relevant memory",
              "criteria": {"c0": "User prefers everyday examples", "c1": "Unrelated information"}}}}),
    ]
    with httpx.Client(timeout=30, trust_env=True) as client:
        passed = []
        for label, url, key, body in probes:
            try:
                headers = {"Authorization": "Bearer " + key}
                response = client.post(url, headers=headers, json=body) if body else client.get(url, headers=headers)
                print(label, response.status_code)
                if response.is_success:
                    data = response.json()
                    if label == "model":
                        print("luna_models", [x["id"] for x in data.get("data", []) if "luna" in x.get("id", "").lower()])
                    else:
                        print("items", len(data.get("data", data.get("results", data.get("answers", {})))))
                        if label == "embedding":
                            dimension = len(data["data"][0]["embedding"])
                            assert dimension == int(config["HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS"])
                            print("dimensions", dimension)
                    passed.append(label)
            except Exception as exc:
                print(label, type(exc).__name__)
        if len(passed) != len(probes):
            raise SystemExit(1)
    import asyncio
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server" / "backend"))
    from app.memory.style import explicit_style_feedback
    signal = asyncio.run(explicit_style_feedback("这个比喻太绕，请用更生活化的例子。", "synthetic-live"))
    assert signal.explicit and signal.aspect == "examples"
    print("native PydanticAI style filter PASS")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("provider verification FAIL", type(exc).__name__)
        raise SystemExit(1) from None
