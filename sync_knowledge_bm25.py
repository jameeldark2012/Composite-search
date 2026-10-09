"""Synchronize one Open WebUI Knowledge Base into the local Tantivy index."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path


async def sync_knowledge_base(knowledge_id: str) -> dict:
    os.environ.setdefault("FROM_INIT_PY", "true")
    if not os.getenv("WEBUI_SECRET_KEY") and os.getenv("WEBUI_AUTH", "true").lower() == "true":
        key_candidates = (Path.cwd() / ".webui_secret_key", Path.home() / ".webui_secret_key")
        key_path = next((candidate for candidate in key_candidates if candidate.is_file()), None)
        if key_path is None:
            raise RuntimeError(
                "Open WebUI requires WEBUI_SECRET_KEY. Set it in the environment or run this "
                "script from the directory containing Open WebUI's .webui_secret_key file."
            )
        os.environ["WEBUI_SECRET_KEY"] = key_path.read_text(encoding="utf-8")

    from open_webui.models.knowledge import Knowledges
    from open_webui.models.users import Users

    from open_webui_bm25_tool import Tools

    knowledge = await Knowledges.get_knowledge_by_id(knowledge_id)
    if knowledge is None:
        raise ValueError(f"Knowledge Base {knowledge_id!r} was not found.")

    owner = await Users.get_user_by_id(knowledge.user_id)
    if owner is None:
        raise ValueError(f"Knowledge Base owner {knowledge.user_id!r} was not found.")

    response = json.loads(
        await Tools().sync_knowledge_bm25_index(
            knowledge_id=knowledge_id,
            __user__=owner.model_dump(),
        )
    )
    if "error" in response:
        raise RuntimeError(response["error"])
    return response


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build or replace the local Tantivy index for an Open WebUI Knowledge Base."
    )
    parser.add_argument("knowledge_id", help="Open WebUI Knowledge Base ID")
    args = parser.parse_args()
    result = asyncio.run(sync_knowledge_base(args.knowledge_id))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
