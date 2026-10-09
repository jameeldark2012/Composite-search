import tempfile
import unittest
from pathlib import Path

from open_webui_bm25_tool import (
    _format_results,
    _literal_file_matches,
    _merge_search_results,
    _search_index,
    _source_passages,
    _write_index,
    iter_passages,
    normalize_text,
    query_terms,
)


class FileRecord:
    def __init__(self, file_id, filename, content):
        self.id = file_id
        self.filename = filename
        self.data = {"content": content}


class DirectoryRecord:
    def __init__(self, directory_id, name, parent_id=None):
        self.id = directory_id
        self.name = name
        self.parent_id = parent_id


class ArabicEnglishBm25Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.index_path = Path(self.temp_dir.name) / "index"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_arabic_marks_tatweel_and_alef_variants_normalize(self):
        self.assertEqual(normalize_text("آلأُمَّــةُ"), normalize_text("الامة"))

    def test_morphological_forms_are_not_aggressively_stemmed(self):
        self.assertNotEqual(normalize_text("ترث"), normalize_text("يرث"))
        self.assertNotEqual(normalize_text("عبد"), normalize_text("عبادة"))

    def test_passage_is_original_text_and_lines_are_reliable(self):
        source = "العنوان\n" + ("معلومة عربية. " * 100)
        chunk = next(iter_passages(source, max_chars=200, overlap=20))
        excerpt_start = source.index(chunk["excerpt"])
        self.assertEqual(source[excerpt_start : excerpt_start + len(chunk["excerpt"])], chunk["excerpt"])
        self.assertEqual(chunk["line_start"], 1)
        self.assertEqual(chunk["line_end"], 2)

    def test_arabic_query_finds_original_excerpt_and_attribution(self):
        source = "أَنَّ الأُمَّةَ إذا مَلَكَتْ سَيِّدَها لا تَرِثُهُ."
        passages, skipped = _source_passages([(
            FileRecord("file-ar", "fiqh.txt", source),
            "dir-1",
        )], [DirectoryRecord("dir-1", "فقه")])
        self.assertEqual(skipped, 0)
        _write_index(self.index_path, iter(passages))

        results = _search_index(self.index_path, "آلأُمّة", 5)

        self.assertTrue(results)
        self.assertEqual(results[0]["source_id"], "file-ar")
        self.assertEqual(results[0]["filename"], "fiqh.txt")
        self.assertEqual(results[0]["source_path"], "فقه/fiqh.txt")
        self.assertEqual(results[0]["excerpt"], source)

    def test_english_and_mixed_language_terms_retrieve_matching_passages(self):
        documents = [
            {
                "file_id": "en",
                "filename": "manual.md",
                "source_path": "manual.md",
                "line_start": 1,
                "line_end": 1,
                "excerpt": "A lexical search engine ranks passages using BM25.",
            },
            {
                "file_id": "mix",
                "filename": "mixed.txt",
                "source_path": "arabic/mixed.txt",
                "line_start": 4,
                "line_end": 4,
                "excerpt": "يعتمد البحث lexical ranking على كلمات السؤال.",
            },
        ]
        _write_index(self.index_path, documents)

        english = _search_index(self.index_path, "lexical engine", 5)
        mixed = _search_index(self.index_path, "البحث lexical", 5)

        self.assertEqual(english[0]["source_id"], "en")
        self.assertEqual(mixed[0]["source_id"], "mix")

    def test_complex_question_finds_alternative_terminology_and_literal_comparison(self):
        source = "الأمة لا سهم لها في التركة بعد وفاة مولاها."
        documents = [
            {
                "file_id": "fiqh",
                "filename": "inheritance.txt",
                "source_path": "inheritance.txt",
                "line_start": 1,
                "line_end": 1,
                "excerpt": source,
            },
            {
                "file_id": "noise",
                "filename": "other.txt",
                "source_path": "other.txt",
                "line_start": 1,
                "line_end": 1,
                "excerpt": "أحكام البيع والشراء في الأسواق.",
            },
        ]
        _write_index(self.index_path, documents)

        bm25 = _search_index(self.index_path, "هل ترث الأمة سيدها؟", 5)
        literal_found = any("هل ترث الأمة سيدها" in item["excerpt"] for item in documents)

        self.assertTrue(bm25)
        self.assertEqual(bm25[0]["source_id"], "fiqh")
        self.assertFalse(literal_found)
        self.assertIn("الأمة لا سهم لها", bm25[0]["excerpt"])

    def test_query_tokenization_ignores_operator_syntax(self):
        self.assertEqual(query_terms('أمة OR "سيدها"'), ["امة", "or", "سيدها"])

    def test_literal_matches_return_original_line_and_attribution(self):
        content = "عنوان\nهذه فقرة عن الأُمَّةِ وسيدها.\nسطر آخر."
        file = FileRecord("literal-ar", "fiqh.txt", content)
        matches = _literal_file_matches(
            file,
            "dir-1",
            {"dir-1": "فقه"},
            lambda line: "الأُمَّةِ" in line,
            5,
        )

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["source_id"], "literal-ar")
        self.assertEqual(matches[0]["source_path"], "فقه/fiqh.txt")
        self.assertEqual(matches[0]["line_start"], 2)
        self.assertEqual(matches[0]["excerpt"], "هذه فقرة عن الأُمَّةِ وسيدها.")

    def test_long_literal_match_excerpt_keeps_the_matched_text(self):
        phrase = "needle phrase"
        content = ("x" * 1_500) + phrase + ("y" * 1_500)
        matches = _literal_file_matches(
            FileRecord("long", "long.txt", content),
            None,
            {},
            lambda line: phrase in line,
            1,
            pattern=phrase,
            case_insensitive=False,
        )

        self.assertEqual(len(matches), 1)
        self.assertLessEqual(len(matches[0]["excerpt"]), 1_100)
        self.assertIn(phrase, matches[0]["excerpt"])

    def test_result_formatter_deduplicates_and_preserves_bm25_score(self):
        result = {
            "source_id": "file",
            "filename": "book.txt",
            "source_path": "book.txt",
            "line_start": 2,
            "line_end": 2,
            "excerpt": "An original passage.",
            "score": 1.25,
        }
        formatted = _format_results([result, result], 5)

        self.assertEqual(len(formatted), 1)
        self.assertEqual(formatted[0]["score"], 1.25)
        self.assertEqual(formatted[0]["excerpt"], "An original passage.")

    def test_hybrid_results_merge_duplicates_and_keep_both_method_labels(self):
        bm25 = [{
            "source_id": "file",
            "filename": "book.txt",
            "source_path": "book.txt",
            "line_start": 2,
            "line_end": 2,
            "excerpt": "الأمة لا ترث سيدها.",
            "score": 2.0,
        }]
        literal = [{
            "source_id": "file",
            "filename": "book.txt",
            "source_path": "book.txt",
            "line_start": 2,
            "line_end": 2,
            "excerpt": "الأمة لا ترث سيدها.",
        }]

        results = _merge_search_results(bm25, literal, 5)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["methods"], ["bm25", "literal"])
        self.assertEqual(results[0]["bm25_rank"], 1)
        self.assertEqual(results[0]["score"], 2.0)

    def test_hybrid_results_merge_literal_line_inside_bm25_passage(self):
        bm25 = [{
            "source_id": "file",
            "filename": "book.txt",
            "source_path": "book.txt",
            "line_start": 10,
            "line_end": 20,
            "excerpt": "A wider BM25 passage.",
            "score": 2.0,
        }]
        literal = [{
            "source_id": "file",
            "filename": "book.txt",
            "source_path": "book.txt",
            "line_start": 15,
            "line_end": 15,
            "excerpt": "The exact literal line.",
        }]

        results = _merge_search_results(bm25, literal, 5)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["methods"], ["bm25", "literal"])
        self.assertEqual(results[0]["literal_lines"], ["15-15"])


if __name__ == "__main__":
    unittest.main()
