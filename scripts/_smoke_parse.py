"""Parser-only smoke test: parse a PDF with MinerU via RAGAnything, no LLM calls."""
import asyncio
import sys
from pathlib import Path
from raganything import RAGAnything


async def main(pdf_path: str):
    rag = RAGAnything()
    content_list, md = await rag.parse_document(
        file_path=pdf_path,
        output_dir="./output",
        parse_method="auto",
        display_stats=True,
        device="cpu",
    )
    print(f"\n[OK] blocks={len(content_list)} md_chars={len(md)}")
    types = {}
    for it in content_list:
        if isinstance(it, dict):
            t = it.get("type", "unknown")
            types[t] = types.get(t, 0) + 1
    print(f"[types] {types}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
