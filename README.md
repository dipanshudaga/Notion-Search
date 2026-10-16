# Notion Search

Notion's built-in search often misses what you are looking for. It leans on exact words and titles, it can skip text buried inside long pages, toggles and database rows, and its ranking is hard to predict.

Notion Search keeps the same Cmd+K box and replaces what answers it. Every page in your workspace is indexed on your Mac, and each query is answered by a hybrid of exact-word search and meaning-based search. The meaning side uses Google's **EmbeddingGemma 2** model, running locally on the Apple Silicon GPU. Nothing leaves your machine, and it keeps working offline.

## Quick start

```bash
uvx notion-search
```

That one command does everything: it installs the program, downloads the model, indexes your pages and starts the background service. Afterwards, run `notion-search` any time to open the menu. (Running `uvx notion-search` again also works: it hands over to the installed copy.)

No `uv` yet? Use the one-line installer below. It installs `uv` for you.

## Install

You need a Mac with Apple Silicon (M1 or newer) and Notion Desktop in `/Applications`. Open Terminal and run:

```bash
curl -fsSL https://raw.githubusercontent.com/dipanshudaga/Notion-Search/main/install.sh | bash
```

The first run takes 5 to 10 minutes and shows four numbered steps with progress bars:

1. **Installing the notion-search command.** `uv` (installed first if you do not have it) fetches Python 3.12 and the package into their own isolated environment. Git, Homebrew and Xcode are not required.
2. **Downloading the model.** EmbeddingGemma 2 comes from Hugging Face: about 900 MB, once, no account needed. The audio part of the model, which this app does not use, is skipped. The model is stored in the app's own folder.
3. **Indexing pages.** It reads your pages from Notion's local database and builds the search index.
4. **Starting background services.** It registers a service that starts at every login.

Prefer to install the program first? Run `uv tool install notion-search` (or `pipx install notion-search`), then run `notion-search`. The first run sets everything up, and later runs open the menu. Plain `pip install` fails on current Macs with Homebrew's Python, because that Python is "externally managed". Use `uv` or `pipx`, which give the app its own environment.

Plan for about 2 GB of free disk space. When the screen says "All set", you are done.

## Using it

Open Notion and press **Cmd+K** (or Cmd+P). Nothing else changes.

Notion has to be started with a debug port so the service can answer its search requests. If you open Notion normally, the service notices within a couple of seconds, quits it, and reopens it with the port enabled. Expect one short restart each time you launch Notion from the Dock or Spotlight. Your pages are safe, because Notion saves continuously.

New and edited pages are picked up automatically, usually within 15 seconds. To get a newer version of Notion Search itself, run `notion-search` and choose **Update**.

## What gets searched

- The full text of every page, however long, including text inside toggles, columns, lists and callouts.
- Table cells.
- Page titles, and the breadcrumb path of each page.

Long pages are split into chunks of about 1,500 characters. Each chunk is embedded separately, so a match deep inside a long page still finds the page, and the result snippet shows the part that matched.

Not indexed: database properties such as status, tags or dates, and the contents of PDFs, images and other attachments. Pages Notion Desktop has never loaded onto your Mac are not in its local database, so they cannot be indexed until Notion loads them.

## How results are ranked

Each query is run two ways and the results are merged.

- **Exact words:** SQLite FTS5 with stemming and prefix matching. A title match counts more than a body match.
- **Meaning:** the query is embedded and compared with every chunk. A chunk only counts when its similarity stands well above the typical score for that query (4 standard deviations), which keeps unrelated pages out and lets nonsense queries return nothing.

Pages that contain all of your words are boosted, and titles that contain them are boosted again. A short block that matches your query, such as a toggle titled "Github" when you search "github", puts its page first, and the page you currently have open gets a small boost when it already matches. Under each result you see the one block of the page that matched best, never a run of neighbouring text, with the matching words highlighted.

## The control menu

Run `notion-search` in Terminal for an arrow-key menu:

| Option | What it does |
|---|---|
| Update | Gets the newest release from PyPI, then refreshes the service and index |
| Status | Shows whether the service is running and Notion is hooked |
| Restart | Restarts the background service |
| Reindex | Rebuilds the whole index from scratch (about 2 to 3 minutes) |
| Uninstall | After you confirm, removes everything: the service, index, model and the command itself |

Before anything is installed, the menu is short: Install and Quit. The same actions work as commands: `notion-search install`, `update`, `status`, `restart`, `reindex`, `uninstall [--keep-data] [--yes]`, and `logs` for debugging. If the command is not found, run `uv tool update-shell` and open a new Terminal window.

## Troubleshooting

- **Results look slow or come from Notion's own search:** run `notion-search status`. The service must be running and Notion must be hooked.
- **A new page does not appear:** wait about 15 seconds, and make sure Notion Desktop has opened that page at least once.
- **Results look stale or wrong:** run Reindex from the menu.
- **Something else:** `notion-search logs` follows `~/Library/Logs/NotionSearch.log`.

## Uninstall

Run `notion-search`, choose **Uninstall**, and confirm. It removes everything Notion Search put on your Mac: the background service, the index, the 900 MB model, the log, uv's cached copies and the `notion-search` command itself (about 1.7 GB in all). Notion itself is never modified, so it behaves exactly as before. `uv` stays, because other tools may use it.

From a script: `notion-search uninstall --yes` skips the question, and `--keep-data` removes only the service and keeps the index, model and command.

## Privacy and security

- Everything runs locally. No page text, query or telemetry is sent anywhere. The only download is the model (about 900 MB), once.
- Notion's database is opened read-only, and Notion's app files are never touched, so its code signature stays intact.
- The service talks to Notion over Chromium's DevTools port (127.0.0.1:9222). That port is reachable by any process running as you on this Mac, so do not use this on a Mac that other people control.

## How it works

```
Notion Desktop ── Cmd+K ──> /api/v3/search request
        │  (intercepted over the DevTools port)
        ▼
service.py ──> engine.py ──> answer in Notion's own response format
        │                    │
        │                    ├─ FTS5 index   (exact words)
        │                    └─ vectors      (EmbeddingGemma 2, MLX, 8-bit)
        ▼
indexer.py: reads Notion's local database (read-only),
re-embeds only the pages whose text changed
```

| File | Role |
|---|---|
| `src/notion_search/cli.py` | The `notion-search` command: menu, install, status, reindex, uninstall |
| `src/notion_search/service.py` | Background service: keeps Notion hooked, answers searches, keeps the index fresh |
| `src/notion_search/engine.py` | Hybrid retrieval, ranking, snippets, and Notion's response format |
| `src/notion_search/indexer.py` | Reads the Notion database, builds pages and chunks, stores the index |
| `src/notion_search/embedder.py` | Runs EmbeddingGemma 2 on the GPU with the model card's prompts |
| `install.sh` | One-line bootstrap: installs `uv`, then the package, then runs `notion-search install` |

The index and the model live in `~/Library/Application Support/NotionSearch`. All model work runs on a single thread, because MLX streams are per-thread.

## Performance

Measured on an M2 MacBook Air with a 836-page workspace (956 chunks):

- Search: about 20 to 50 ms per query, including the model call. The first query after a restart takes about 250 ms.
- Memory: about 790 MB resident, most of it the model in 8-bit form (280 MB) and Python.
- Full index build: 2 to 3 minutes. Later updates re-embed only changed pages and take about a second.
- Idle: no measurable CPU, and no network traffic beyond local connections to Notion.

## Limitations

- Apple Silicon Macs only (MLX), and Notion Desktop for Mac. It does not work with Notion in a browser.
- Pages must be cached by Notion Desktop to be searchable.
- Notion restarts once when it is opened without the debug port.
- Database properties and attachment contents are not searched.

## Credits

- **[DigUp](https://github.com/ARahim3/DigUp)** by ARahim3 (MIT). DigUp, which searches local files with the same model, shaped several decisions here: the model card's query and document prompts, scoring a hit against the whole score distribution for a query instead of a fixed cutoff, storing a model fingerprint in the index so a model change triggers a rebuild, and the choice of `mlx-vlm` release that supports the model.
- **[EmbeddingGemma 2](https://huggingface.co/google/embeddinggemma-2)** by Google DeepMind (Apache 2.0).
- **[MLX](https://github.com/ml-explore/mlx)** by Apple and **[mlx-vlm](https://github.com/Blaizzy/mlx-vlm)** by Blaizzy.
- **SQLite FTS5** for exact-word search.

Notion Search is not affiliated with Notion Labs, Google or Apple. Notion is a trademark of Notion Labs, Inc.
