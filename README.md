# Local Arabic + English BM25 for Open WebUI

This adds a local Tantivy BM25 tool alongside Open WebUI's existing
`grep_knowledge_files` literal/regex tool. It does not replace that tool, write
to Knowledge Base files, rebuild embeddings, or start a search server.

## Verified environment

The integration was checked against Open WebUI **0.11.4** running on Windows
with **Python 3.12.7**. That release exposes Knowledge Base extracted text as
`File.data["content"]`, along with the source filename, file ID, metadata and
directory relation. The tool uses the installed Open WebUI models and
`knowledge_fs` access checks to read this content; it does not scrape an
assumed storage path or use a separate source library.

`tantivy==0.26.2` publishes a `cp312-cp312-win_amd64` wheel. It was installed
and imported in the same Python environment as the running Open WebUI
installation. The index is stored under Open WebUI's configured
`DATA_DIR\tantivy_bm25` by default. The Tantivy package and index are local;
after setup and synchronization, searching requires no internet access.

## Windows setup

1. Open PowerShell in this project folder. Install Tantivy into the **same
   Python environment that launches Open WebUI** (do not use a separate
   interpreter):

   ```powershell
   & "C:\path\to\open-webui-python.exe" -m pip install -r requirements.txt
   ```

   For the environment verified here:

   ```powershell
   & "C:\Users\jamee\.PYENV\PYENV-WIN\versions\3.12.7\python.exe" -m pip install -r requirements.txt
   ```

   Confirm the native package loads:

   ```powershell
   & "C:\path\to\open-webui-python.exe" -c "import tantivy; print(tantivy.tantivy)"
   ```

2. In Open WebUI, open **Workspace → Tools**, create a tool, and paste the
   complete contents of [`open_webui_bm25_tool.py`](./open_webui_bm25_tool.py).
   Save and enable it. Restart Open WebUI if it was already running when you
   installed Tantivy.

3. Keep `INDEX_DIR` blank to use the Open WebUI `DATA_DIR` automatically.
   Optionally set it to another local directory with sufficient disk space.

4. Find the Knowledge Base ID in the Knowledge Base page or use the existing
   `list_knowledge_bases` tool. Ensure the model can access that Knowledge
   Base. Call `sync_knowledge_bm25_index` once with its ID. The function
   verifies the caller's access and synchronizes only that base.

5. Enable the new BM25 tool for the model. Use `search_knowledge_hybrid` for
   BM25-ranked passages and literal/regex matches in one call. The original
   `grep_knowledge_files` tool remains available for standalone exact/regex
   searches.

## On-demand Windows synchronization

Double-click [`sync_knowledge_bm25.bat`](./sync_knowledge_bm25.bat) and enter
the Knowledge Base ID for a simple on-demand sync. The batch file points to the
Python 3.12.7 interpreter verified for this installation; edit its `PYTHON_EXE`
line if Open WebUI uses a different interpreter. You can also run the Python
script directly from PowerShell whenever Knowledge Base files are added,
edited, or deleted:

```powershell
& "C:\Users\jamee\.PYENV\PYENV-WIN\versions\3.12.7\python.exe" `
  ".\sync_knowledge_bm25.py" `
  "YOUR-KNOWLEDGE-BASE-ID"
```

Replace the interpreter path with the Python executable used by your Open
WebUI installation. The script checks the Knowledge Base and synchronizes as
its owner, so the owner must still exist. When `WEBUI_SECRET_KEY` is not
already set, it reuses the standard `.webui_secret_key` file from the current
directory or user home without displaying it. The in-chat
`sync_knowledge_bm25_index` function remains available as another option.

## Synchronization and search behavior

- Call `sync_knowledge_bm25_index` after adding, editing, or deleting files.
  Synchronization replaces that Knowledge Base's local index from Open WebUI's
  current extracted text; it does not change source files or vector data.
- Synchronization streams Knowledge Base records one at a time rather than
  loading every extracted document into memory at once. Open WebUI displays
  progress updates as files are indexed, and the final response reports file
  and passage counts.
- `search_knowledge_hybrid` runs Tantivy BM25 retrieval and Open WebUI's
  existing `knowledge_fs.build_matcher` literal/RE2 matcher over extracted
  source text in one tool call. Results are deduplicated and labeled with
  `methods` (`bm25`, `literal`, or both); BM25 rank and score are included
  where available. Set `literal_pattern` to search a phrase distinct from the
  BM25 query, or set `use_regex` to explicitly use a regular expression.
  `literal_matches_truncated` indicates when the capped literal scan stopped
  after collecting enough matches. Overlapping source-line ranges are merged
  into one result, with both methods and the literal match line recorded.
- Each Knowledge Base gets its own Tantivy index. Its directory name is a
  SHA-256 of the Knowledge Base ID, not the ID itself.
- Search is limited to Knowledge Bases the current user can access. When a
  model has attached Knowledge Bases, only those are eligible. If more than one
  eligible base exists, pass `knowledge_id` to select one.
- Excerpts are original extracted text, split into bounded passages. File IDs,
  filename/path and source line ranges are preserved. Page or chapter labels
  are not invented when Open WebUI does not provide them reliably.
- Arabic diacritics and tatweel are removed; alef variants (أ، إ، آ، ٱ) are
  folded to ا, and final alef maksura is folded to ي. NFKC and case folding
  support English and mixed-language input. The original excerpt is stored
  separately and never normalized for display.
- Arabic root stemming is deliberately not enabled. A local check with
  NLTK's ISRI stemmer mapped `عبد` (servant/slave) and `عبادة` (worship) to the
  same stem, while `ترث` and `يرث` remained different. That is both a concrete
  false-match risk and weak coverage for inflected forms. Conservative
  orthographic normalization avoids that false positive; stemming can be
  reconsidered against the actual collection and labeled relevance judgments.
- Search output is limited to eight results, at most 1,100 excerpt characters
  each, and 7,000 total excerpt characters.

## Tests and evaluation

Run the focused tests with the same interpreter after installing requirements:

```powershell
& "C:\path\to\open-webui-python.exe" -m unittest discover -s tests -v
```

They cover Arabic diacritics/letter variants, preservation of original
passages, English and mixed-language retrieval, source attribution and line
information, and the complex query **«هل ترث الأمة سيدها؟»** against a
synthetic alternative-wording fixture. That fixture demonstrates lexical
retrieval when the source says “لا سهم لها في التركة ... مولاها”; it is not
evidence about the contents of any particular Knowledge Base.

For the current live **Islamic library** Knowledge Base, the mandatory complex
query was run against BM25 and `grep_knowledge_files`; then the combined tool
was tested with an exact heading that appears in the retrieved book. Assess
relevance separately from whether an LLM interprets the cited passage
correctly.

| Query | Relevant passage retrieved? | BM25 rank | Literal search found it? | Combined retrieval improved? | False/irrelevant matches |
|---|---|---:|---|---|---|
| A verbatim source phrase | Not evaluated against a live Knowledge Base | — | — | — | — |
| Alternative terminology | Not evaluated against a live Knowledge Base | — | — | — | — |
| Arabic spelling variant / inflection | Not evaluated against a live Knowledge Base | — | — | — | — |
| English query | Not evaluated against a live Knowledge Base | — | — | — | — |

Live source attribution was verified as
`Noor-Book.com عمدة الفقه في المذهب الحنبلي.pdf`, source ID
`c66f7298-42bb-4a13-b1ae-d5cfc2a92c44`, lines 1439–1461. BM25's excerpt was
original extracted text. The first result discusses the status of an
`أمّ الولد`; it should not be treated as directly answering the inheritance
question without further evidence. A separate exact search for the heading
`باب أحكام أمهات الأولاد` found two occurrences in that file, at lines 1446 and
2711. Before line-overlap deduplication was added, the combined run displayed
the overlapping heading and BM25 passage separately; the code now merges
overlapping source lines and labels the result with both methods.

The tests in this repository use a controlled fixture. The live Open WebUI API
was also used to verify synchronization and hybrid search against the current
authenticated Knowledge Base. One actual CLI re-sync completed with
`files_seen: 2` and `indexed_passages: 1372`. English and mixed-language
behavior is covered by fixture tests; those query categories and a live
offline/network-disconnected run have not yet been measured against this
Knowledge Base.
