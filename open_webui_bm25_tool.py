"""Local Tantivy BM25 search for extracted Open WebUI Knowledge Base text."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
import unicodedata
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import tantivy
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

CHUNK_CHARS = 1_100
CHUNK_OVERLAP = 140
MAX_RESULTS = 8
MAX_EXCERPT_CHARS = 1_100
MAX_TOTAL_EXCERPT_CHARS = 7_000
QUERY_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
ARABIC_MARKS_RE = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")
ARABIC_ALEF_VARIANTS = str.maketrans(
    {
        "\u0622": "\u0627",
        "\u0623": "\u0627",
        "\u0625": "\u0627",
        "\u0671": "\u0627",
        "\u0649": "\u064a",
    }
)

_LOCKS: dict[str, threading.RLock] = {}
_SYNC_LOCKS: dict[str, asyncio.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def normalize_text(text: str) -> str:
    """Normalize search text without changing the stored source excerpt."""
    normalized = unicodedata.normalize("NFKC", text)
    normalized = ARABIC_MARKS_RE.sub("", normalized)
    normalized = normalized.replace("\u0640", "")
    return normalized.translate(ARABIC_ALEF_VARIANTS).casefold()


def query_terms(query: str) -> list[str]:
    """Return escaped single-token query terms, avoiding Tantivy query syntax."""
    return QUERY_TOKEN_RE.findall(normalize_text(query))


def iter_passages(text: str, max_chars: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP):
    """Yield original-text passages and their one-based source line bounds."""
    start = 0
    line_at_start = 1
    text_length = len(text)
    while start < text_length:
        end = min(start + max_chars, text_length)
        if end < text_length:
            lower_bound = min(start + max_chars * 2 // 3, end)
            boundaries = [
                text.rfind("\n\n", lower_bound, end),
                text.rfind("\n", lower_bound, end),
                text.rfind(". ", lower_bound, end),
                text.rfind("؟", lower_bound, end),
                text.rfind("! ", lower_bound, end),
                text.rfind("? ", lower_bound, end),
                text.rfind(" ", lower_bound, end),
            ]
            boundary = max(boundaries)
            if boundary >= lower_bound:
                end = boundary + (2 if text.startswith("\n\n", boundary) else 1)

        excerpt_start = start
        excerpt_end = end
        while excerpt_start < excerpt_end and text[excerpt_start].isspace():
            excerpt_start += 1
        while excerpt_end > excerpt_start and text[excerpt_end - 1].isspace():
            excerpt_end -= 1

        if excerpt_start < excerpt_end:
            yield {
                "excerpt": text[excerpt_start:excerpt_end],
                "line_start": line_at_start + text.count("\n", start, excerpt_start),
                "line_end": line_at_start + text.count("\n", start, excerpt_end),
            }

        if end >= text_length:
            break
        next_start = max(start + 1, end - overlap)
        line_at_start += text.count("\n", start, next_start)
        start = next_start


def _schema() -> tantivy.Schema:
    builder = tantivy.SchemaBuilder()
    builder.add_text_field("body")
    builder.add_text_field("excerpt", stored=True)
    builder.add_text_field("file_id", stored=True)
    builder.add_text_field("filename", stored=True)
    builder.add_text_field("source_path", stored=True)
    builder.add_integer_field("line_start", stored=True)
    builder.add_integer_field("line_end", stored=True)
    return builder.build()


SCHEMA = _schema()


def _index_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def _index_sync_lock(path: Path) -> asyncio.Lock:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        return _SYNC_LOCKS.setdefault(key, asyncio.Lock())


def _add_passage(writer: Any, passage: dict[str, Any]) -> None:
    document = tantivy.Document()
    document.add_text("body", normalize_text(passage["excerpt"]))
    document.add_text("excerpt", passage["excerpt"])
    document.add_text("file_id", passage["file_id"])
    document.add_text("filename", passage["filename"])
    document.add_text("source_path", passage["source_path"])
    document.add_integer("line_start", passage["line_start"])
    document.add_integer("line_end", passage["line_end"])
    writer.add_document(document)


def _write_index(index_path: Path, passages: Iterable[dict[str, Any]]) -> int:
    index_path.mkdir(parents=True, exist_ok=True)
    lock = _index_lock(index_path)
    with lock:
        if tantivy.Index.exists(str(index_path)):
            index = tantivy.Index.open(str(index_path))
        else:
            index = tantivy.Index(schema=SCHEMA, path=str(index_path), reuse=False)

        writer = index.writer(heap_size=64_000_000, num_threads=1)
        indexed_count = 0
        try:
            writer.delete_all_documents()
            for passage in passages:
                _add_passage(writer, passage)
                indexed_count += 1
            writer.commit()
        except Exception:
            writer.rollback()
            raise
        index.reload()
        return indexed_count


def _search_index(index_path: Path, query: str, limit: int) -> list[dict[str, Any]]:
    terms = query_terms(query)
    if not terms:
        return []

    safe_query = " OR ".join(f'"{term}"' for term in terms[:64])
    lock = _index_lock(index_path)
    with lock:
        index = tantivy.Index.open(str(index_path))
        parsed = index.parse_query(safe_query, ["body"])
        searcher = index.searcher()
        result = searcher.search(parsed, limit)
        passages = []
        for score, address in result.hits:
            document = searcher.doc(address)
            passages.append(
                {
                    "score": float(score),
                    "excerpt": document.get_first("excerpt"),
                    "source_id": document.get_first("file_id"),
                    "filename": document.get_first("filename"),
                    "source_path": document.get_first("source_path"),
                    "line_start": document.get_first("line_start"),
                    "line_end": document.get_first("line_end"),
                }
            )
        return passages


def _relative_directory_paths(directories: list[Any]) -> dict[str, str]:
    directory_by_id = {directory.id: directory for directory in directories}
    result = {}

    for directory_id, directory in directory_by_id.items():
        components = [directory.name]
        parent_id = directory.parent_id
        visited = {directory_id}
        while parent_id and parent_id in directory_by_id and parent_id not in visited:
            visited.add(parent_id)
            parent = directory_by_id[parent_id]
            components.append(parent.name)
            parent_id = parent.parent_id
        result[directory_id] = "/".join(reversed(components))
    return result


def _source_passages(files: list[Any], directories: list[Any]) -> tuple[list[dict[str, Any]], int]:
    directory_paths = _relative_directory_paths(directories)
    passages = []
    skipped_without_text = 0

    for file_model, directory_id in files:
        file_passages, skipped = _file_passages(file_model, directory_id, directory_paths)
        if skipped:
            skipped_without_text += 1
        passages.extend(file_passages)
    return passages, skipped_without_text


def _file_passages(
    file_model: Any,
    directory_id: str | None,
    directory_paths: dict[str, str],
) -> tuple[list[dict[str, Any]], bool]:
    data = file_model.data or {}
    content = data.get("content")
    if not isinstance(content, str) or not content.strip():
        return [], True

    parent = directory_paths.get(directory_id, "") if directory_id else ""
    source_path = f"{parent}/{file_model.filename}" if parent else file_model.filename
    passages = [
        {
            **passage,
            "file_id": file_model.id,
            "filename": file_model.filename,
            "source_path": source_path,
        }
        for passage in iter_passages(content)
        if query_terms(passage["excerpt"])
    ]
    return passages, False


def _literal_file_matches(
    file_model: Any,
    directory_id: str | None,
    directory_paths: dict[str, str],
    match_fn: Any,
    limit: int,
    pattern: str | None = None,
    case_insensitive: bool = True,
) -> list[dict[str, Any]]:
    data = file_model.data or {}
    content = data.get("content")
    if not isinstance(content, str) or not content:
        return []

    parent = directory_paths.get(directory_id, "") if directory_id else ""
    source_path = f"{parent}/{file_model.filename}" if parent else file_model.filename
    matches = []
    offset = 0
    for line_number, line in enumerate(content.splitlines(keepends=True), start=1):
        line_text = line.rstrip("\r\n")
        if match_fn(line_text):
            if pattern:
                haystack = line_text.casefold() if case_insensitive else line_text
                needle = pattern.casefold() if case_insensitive else pattern
                match_at = haystack.find(needle)
            else:
                match_at = -1
            excerpt_start = offset + max(
                0,
                min(
                    max(0, len(line_text) - MAX_EXCERPT_CHARS),
                    match_at - (MAX_EXCERPT_CHARS - len(pattern or "")) // 2,
                ),
            )
            excerpt_end = min(offset + len(line_text), excerpt_start + MAX_EXCERPT_CHARS)
            matches.append(
                {
                    "source_id": file_model.id,
                    "filename": file_model.filename,
                    "source_path": source_path,
                    "line_start": line_number,
                    "line_end": line_number,
                    "excerpt": content[excerpt_start:excerpt_end],
                }
            )
            if len(matches) >= limit:
                return matches
        offset += len(line)
    return matches


def _format_results(results: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    output = []
    chars_used = 0
    seen = set()
    for rank, result in enumerate(results, start=1):
        excerpt = result["excerpt"]
        dedupe_key = (
            result["source_id"],
            " ".join(normalize_text(excerpt).split()),
        )
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        remaining = MAX_TOTAL_EXCERPT_CHARS - chars_used
        if remaining <= 0 or len(output) >= limit:
            break
        excerpt = excerpt[: min(MAX_EXCERPT_CHARS, remaining)]
        item = {
            "rank": rank,
            "filename": result["filename"],
            "source_path": result["source_path"],
            "source_id": result["source_id"],
            "lines": f'{result["line_start"]}-{result["line_end"]}',
            "excerpt": excerpt,
        }
        if "score" in result:
            item["score"] = round(result["score"], 6)
        output.append(item)
        chars_used += len(excerpt)
    return output


def _merge_search_results(
    bm25_results: list[dict[str, Any]],
    literal_results: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    combined = []
    by_source: dict[str, list[tuple[int, int, dict[str, Any]]]] = {}
    chars_used = 0

    for method, results in (("bm25", bm25_results), ("literal", literal_results)):
        for source_rank, result in enumerate(results, start=1):
            excerpt = result["excerpt"][:MAX_EXCERPT_CHARS]
            line_start = result["line_start"]
            line_end = result["line_end"]
            overlapping = next(
                (
                    item
                    for existing_start, existing_end, item in by_source.get(result["source_id"], [])
                    if line_start <= existing_end and existing_start <= line_end
                ),
                None,
            )
            existing = overlapping
            if existing:
                if method not in existing["methods"]:
                    existing["methods"].append(method)
                if method == "literal":
                    literal_lines = existing.setdefault("literal_lines", [])
                    line_label = f"{line_start}-{line_end}"
                    if line_label not in literal_lines:
                        literal_lines.append(line_label)
                continue
            if len(combined) >= limit:
                continue

            remaining = MAX_TOTAL_EXCERPT_CHARS - chars_used
            if remaining <= 0:
                return combined
            excerpt = excerpt[:remaining]
            item = {
                "rank": len(combined) + 1,
                "methods": [method],
                "filename": result["filename"],
                "source_path": result["source_path"],
                "source_id": result["source_id"],
                "lines": f'{result["line_start"]}-{result["line_end"]}',
                "excerpt": excerpt,
            }
            if method == "bm25":
                item["bm25_rank"] = source_rank
                item["score"] = round(result["score"], 6)
            else:
                item["literal_lines"] = [f"{line_start}-{line_end}"]
            by_source.setdefault(result["source_id"], []).append((line_start, line_end, item))
            combined.append(item)
            chars_used += len(excerpt)
    return combined


def _index_directory(base_path: Path, knowledge_id: str) -> Path:
    key = hashlib.sha256(knowledge_id.encode("utf-8")).hexdigest()
    return base_path / key


def _json_response(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Tools:
    class Valves(BaseModel):
        INDEX_DIR: str = Field(
            default="",
            description="Optional local index root. Defaults to Open WebUI DATA_DIR/tantivy_bm25.",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _base_path(self) -> Path:
        if self.valves.INDEX_DIR.strip():
            return Path(self.valves.INDEX_DIR).expanduser().resolve()
        from open_webui.env import DATA_DIR

        return Path(DATA_DIR) / "tantivy_bm25"

    async def _allowed_knowledge_bases(
        self,
        user: dict | None,
        model_knowledge: list[dict] | None,
        knowledge_id: str | None,
    ) -> list[tuple[str, str, str]]:
        if not user or not user.get("id"):
            return []
        from open_webui.tools.knowledge_fs import _get_accessible_kb_ids

        return await _get_accessible_kb_ids(user, model_knowledge, knowledge_id)

    async def _select_knowledge_base(
        self,
        user: dict | None,
        model_knowledge: list[dict] | None,
        knowledge_id: str | None,
    ) -> tuple[tuple[str, str, str] | None, str | None]:
        accessible = await self._allowed_knowledge_bases(user, model_knowledge, knowledge_id)
        if knowledge_id and not accessible:
            return None, "Knowledge Base is unavailable or you do not have read access."
        if not accessible:
            return None, "No accessible Knowledge Bases are attached or available to this user."
        if knowledge_id:
            return accessible[0], None
        if len(accessible) != 1:
            choices = [{"name": name, "knowledge_id": identifier} for identifier, name, _ in accessible]
            return None, f"Specify knowledge_id to search one of these accessible Knowledge Bases: {_json_response(choices)}"
        return accessible[0], None

    async def search_knowledge_bm25(
        self,
        query: str,
        knowledge_id: str | None = None,
        limit: int = 5,
        __user__: dict | None = None,
        __model_knowledge__: list[dict] | None = None,
    ) -> str:
        """Search one accessible Knowledge Base using local Arabic/English BM25.

        Call sync_knowledge_bm25_index first if the index has not been created
        or the Knowledge Base has changed. Existing literal/regex tools remain
        available for exact text and pattern searches.
        """
        if not query or not query.strip():
            return _json_response({"error": "query must contain search terms"})
        if len(query) > 2_048:
            return _json_response({"error": "query exceeds the 2048-character limit"})

        selected, error = await self._select_knowledge_base(__user__, __model_knowledge__, knowledge_id)
        if error:
            return _json_response({"error": error})

        selected_id, selected_name, _ = selected
        index_path = _index_directory(self._base_path(), selected_id)
        if not tantivy.Index.exists(str(index_path)):
            return _json_response(
                {
                    "error": "No BM25 index exists yet. Call sync_knowledge_bm25_index for this Knowledge Base.",
                    "knowledge_id": selected_id,
                    "knowledge_name": selected_name,
                }
            )

        try:
            results = await asyncio.to_thread(
                _search_index,
                index_path,
                query,
                max(1, min(int(limit), MAX_RESULTS)),
            )
            output = _format_results(results, max(1, min(int(limit), MAX_RESULTS)))
            return _json_response(
                {
                    "knowledge_id": selected_id,
                    "knowledge_name": selected_name,
                    "query": query,
                    "results": output,
                }
            )
        except Exception as exc:
            log.exception("Tantivy BM25 search failed for Knowledge Base %s", selected_id)
            return _json_response({"error": f"BM25 search failed: {exc}"})

    async def search_knowledge_hybrid(
        self,
        query: str,
        knowledge_id: str | None = None,
        literal_pattern: str | None = None,
        case_insensitive: bool = True,
        use_regex: bool = False,
        limit: int = 5,
        __user__: dict | None = None,
        __model_knowledge__: list[dict] | None = None,
    ) -> str:
        """Run BM25 and literal/regex search in one call and merge duplicate passages."""
        if not query or not query.strip():
            return _json_response({"error": "query must contain search terms"})
        if len(query) > 2_048:
            return _json_response({"error": "query exceeds the 2048-character limit"})

        pattern = literal_pattern if literal_pattern is not None else query
        if not pattern or len(pattern) > 4_096:
            return _json_response({"error": "literal_pattern must contain at most 4096 characters"})

        selected, error = await self._select_knowledge_base(__user__, __model_knowledge__, knowledge_id)
        if error:
            return _json_response({"error": error})

        selected_id, selected_name, _ = selected
        index_path = _index_directory(self._base_path(), selected_id)
        if not tantivy.Index.exists(str(index_path)):
            return _json_response(
                {
                    "error": "No BM25 index exists yet. Call sync_knowledge_bm25_index for this Knowledge Base.",
                    "knowledge_id": selected_id,
                    "knowledge_name": selected_name,
                }
            )

        try:
            from open_webui.internal.db import get_async_db_context
            from open_webui.models.files import File
            from open_webui.models.knowledge import KnowledgeFile, Knowledges
            from open_webui.tools.knowledge_fs import build_matcher, is_regex_pattern
            from sqlalchemy import select

            match_fn, matcher_error = build_matcher(
                pattern,
                case_insensitive=case_insensitive,
                use_regex=use_regex,
            )
            if matcher_error:
                return _json_response({"error": matcher_error})

            result_limit = max(1, min(int(limit), MAX_RESULTS))
            bm25_task = asyncio.create_task(
                asyncio.to_thread(_search_index, index_path, query, result_limit)
            )
            directories = await Knowledges.get_all_directories(selected_id)
            directory_paths = _relative_directory_paths(directories)
            literal_matches = []
            literal_truncated = False
            stmt = (
                select(File, KnowledgeFile.directory_id)
                .join(KnowledgeFile, File.id == KnowledgeFile.file_id)
                .where(KnowledgeFile.knowledge_id == selected_id)
                .execution_options(yield_per=1)
            )
            async with get_async_db_context() as db:
                result = await db.stream(stmt)
                async for file_model, directory_id in result:
                    file_matches = await asyncio.to_thread(
                        _literal_file_matches,
                        file_model,
                        directory_id,
                        directory_paths,
                        match_fn,
                        result_limit - len(literal_matches),
                        pattern if not (use_regex or is_regex_pattern(pattern)) else None,
                        case_insensitive,
                    )
                    literal_matches.extend(file_matches)
                    if len(literal_matches) >= result_limit:
                        literal_truncated = True
                        break

            bm25_results = await bm25_task
            combined = _merge_search_results(bm25_results, literal_matches, MAX_RESULTS)

            return _json_response(
                {
                    "knowledge_id": selected_id,
                    "knowledge_name": selected_name,
                    "query": query,
                    "literal_pattern": pattern,
                    "bm25_candidates": len(bm25_results),
                    "literal_candidates": len(literal_matches),
                    "literal_matches_truncated": literal_truncated,
                    "results": combined,
                }
            )
        except Exception as exc:
            log.exception("Combined BM25/literal search failed for Knowledge Base %s", selected_id)
            return _json_response({"error": f"Combined search failed: {exc}"})

    async def sync_knowledge_bm25_index(
        self,
        knowledge_id: str,
        __user__: dict | None = None,
        __model_knowledge__: list[dict] | None = None,
        __event_emitter__: Any = None,
    ) -> str:
        """Create or fully synchronize a local Tantivy index from an accessible Knowledge Base.

        Run this after adding, editing, or removing Knowledge Base files. It
        replaces only this BM25 index; it does not modify source files or
        Open WebUI's existing embeddings/vector collections.
        """
        selected, error = await self._select_knowledge_base(__user__, __model_knowledge__, knowledge_id)
        if error:
            return _json_response({"error": error})

        selected_id, selected_name, _ = selected
        try:
            from open_webui.internal.db import get_async_db_context
            from open_webui.models.files import File
            from open_webui.models.knowledge import KnowledgeFile, Knowledges
            from sqlalchemy import select

            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {"description": "Loading Knowledge Base directory metadata...", "done": False},
                    }
                )
            directories = await Knowledges.get_all_directories(selected_id)
            directory_paths = _relative_directory_paths(directories)
            index_path = _index_directory(self._base_path(), selected_id)
            index_path.mkdir(parents=True, exist_ok=True)
            indexed_count = 0
            files_seen = 0
            skipped_without_text = 0
            async with _index_sync_lock(index_path):
                if tantivy.Index.exists(str(index_path)):
                    index = tantivy.Index.open(str(index_path))
                else:
                    index = tantivy.Index(schema=SCHEMA, path=str(index_path), reuse=False)

                writer = index.writer(heap_size=64_000_000, num_threads=1)
                try:
                    writer.delete_all_documents()
                    stmt = (
                        select(File, KnowledgeFile.directory_id)
                        .join(KnowledgeFile, File.id == KnowledgeFile.file_id)
                        .where(KnowledgeFile.knowledge_id == selected_id)
                        .execution_options(yield_per=1)
                    )
                    async with get_async_db_context() as db:
                        result = await db.stream(stmt)
                        async for file_model, directory_id in result:
                            files_seen += 1
                            file_passages, skipped = await asyncio.to_thread(
                                _file_passages, file_model, directory_id, directory_paths
                            )
                            skipped_without_text += int(skipped)
                            for passage in file_passages:
                                _add_passage(writer, passage)
                            indexed_count += len(file_passages)
                            if __event_emitter__ and files_seen % 10 == 0:
                                await __event_emitter__(
                                    {
                                        "type": "status",
                                        "data": {
                                            "description": (
                                                f"Indexed {files_seen} files and {indexed_count} passages..."
                                            ),
                                            "done": False,
                                        },
                                    }
                                )
                    with _index_lock(index_path):
                        writer.commit()
                        index.reload()
                except Exception:
                    writer.rollback()
                    raise

            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": f"Indexed {files_seen} files and {indexed_count} passages.",
                            "done": True,
                        },
                    }
                )
            return _json_response(
                {
                    "knowledge_id": selected_id,
                    "knowledge_name": selected_name,
                    "indexed_passages": indexed_count,
                    "files_seen": files_seen,
                    "files_without_extracted_text": skipped_without_text,
                    "index_path": str(index_path),
                    "message": "BM25 index synchronized from Open WebUI extracted text.",
                }
            )
        except Exception as exc:
            log.exception("Tantivy BM25 synchronization failed for Knowledge Base %s", selected_id)
            return _json_response({"error": f"BM25 synchronization failed: {exc}"})
