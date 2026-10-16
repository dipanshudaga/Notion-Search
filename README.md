# Notion Search

Notion's built-in search often misses what you are looking for. It leans on exact words and titles, it can skip text buried inside long pages, toggles and database rows, and its ranking is hard to predict.

Notion Search keeps the same Cmd+K box and replaces what answers it. Every page in your workspace is indexed on your Mac, and each query is answered by a hybrid of exact-word search and meaning-based search. The meaning side uses Google's **EmbeddingGemma 2** model, running locally on the Apple Silicon GPU. Nothing leaves your machine, and it keeps working offline.

## Quick start

```bash
uv tool install notion-search
notion-search install
```

No `uv` yet? Use the one-line installer below. It installs `uv` for you.

## Install

You need a Mac with Apple Silicon (M1 or newer) and Notion Desktop in `/Applications`. Open Terminal and run:

```bash
curl -fsSL https://raw.githubusercontent.com/dipanshudaga/Notion-Search/main/install.sh | bash
```

The first run takes 5 to 10 minutes. It installs the tool with `uv`, then works through three numbered steps:

0. Installs `uv` if you do not have it. `uv` fetches Python 3.12 and the Notion Search package into their own isolated environment. Git, Homebrew and Xcode are not required.
1. Downloads the EmbeddingGemma 2 model from Hugging Face (about 900 MB, once, no account needed). The audio part of the model, which this app does not use, is skipped. The model is stored in the app's own folder.
2. Reads your pages from Notion's local database and builds the search index.
3. Registers a background service that starts at every login.

Prefer your own Python tooling? The package is on [PyPI](https://pypi.org/project/notion-search/). Install it with `uv tool install notion-search` or `pipx install notion-search`, then run `notion-search install`. Plain `pip install` fails on current Macs with Homebrew's Python, because that Python is "externally managed". Use `uv` or `pipx`, which give the app its own environment.

Plan for about 2 GB of free disk space. When the output says "Background service started", you are done.

## Using it

Open Notion and press **Cmd+K** (or Cmd+P). Nothing else changes.

Notion has to be started with a debug port so the service can answer its search requests. If you open Notion normally, the service notices within a couple of seconds, quits it, and reopens it with the port enabled. Expect one short restart each time you launch Notion from the Dock or Spotlight. Your pages are safe, because Notion saves continuously.

New and edited pages are picked up automatically, usually within 15 seconds. You never need to run an update command.

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

Pages that contain all of your words are boosted, and titles that contain them are boosted again. The snippet shown under each result is cut from the place in the page where your rarest words cluster, with the matches highlighted.

## The control menu

Run `notion-search` in Terminal for an arrow-key menu:

| Option | What it does |
|---|---|
| Update | Reinstalls the service and refreshes dependencies |
| Status | Shows whether the service is running and Notion is hooked |
| Restart | Restarts the background service |
| Reindex | Rebuilds the whole index from scratch (about 2 to 3 minutes) |
| Uninstall | Removes the service, optionally with the index and model |

The same actions work as commands: `notion-search status`, `restart`, `reindex`, `uninstall [--purge]`, and `logs` for debugging. If the command is not found, run `uv tool update-shell` and open a new Terminal window.

## Troubleshooting

- **Results look slow or come from Notion's own search:** run `notion-search status`. The service must be running and Notion must be hooked.
- **A new page does not appear:** wait about 15 seconds, and make sure Notion Desktop has opened that page at least once.
- **Results look stale or wrong:** run Reindex from the menu.
- **Something else:** `notion-search logs` follows `~/Library/Logs/NotionSearch.log`.

## Uninstall

Run `notion-search`, choose Uninstall, then **Service** to keep the index and model or **Everything** to also delete the index and the 900 MB model. To remove the program itself afterwards, run `uv tool uninstall notion-search`. Notion itself is never modified, so it behaves exactly as before.

## Privacy and security

- Everything runs locally. No page text, query or telemetry is sent anywhere. The only download is the model (about 900 MB), once.
- Notion's database is opened read-only, and Notion's app files are never touched, so its code signature stays intact.
- The service talks to Notion over Chromium's DevTools port (127.0.0.1:9222). That port is reachable by any process running as you on this Mac, so do not use this on a Mac that other people control.

## How it works

```
Notion Desktop ── Cmd+K ──> /api/v3/search request
        │  (intercepted over the DevTools port)
        ▼
notion_service.py ──> search_engine.py ──> answer in Notion's own response format
        │                    │
        │                    ├─ FTS5 index   (exact words)
        │                    └─ vectors      (EmbeddingGemma 2, MLX, 8-bit)
        ▼
notion_indexer.py: reads Notion's local database (read-only),
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
- Memory: about 740 MB resident, most of it the model in 8-bit form (280 MB) and Python.
- Full index build: 2 to 3 minutes. Later updates re-embed only changed pages and take about a second.
- Idle: no measurable CPU, and no network traffic beyond local connections to Notion.

## Limitations

- Apple Silicon only (MLX).
- Pages must be cached by Notion Desktop to be searchable.
- Notion restarts once when it is opened without the debug port.
- Database properties and attachment contents are not searched.

## Credits

- **[DigUp](https://github.com/ARahim3/DigUp)** by ARahim3 (MIT). DigUp, which searches local files with the same model, shaped several decisions here: the model card's query and document prompts, scoring a hit against the whole score distribution for a query instead of a fixed cutoff, storing a model fingerprint in the index so a model change triggers a rebuild, and the tested `mlx-vlm` version that supports the model.
- **[EmbeddingGemma 2](https://huggingface.co/google/embeddinggemma-2)** by Google DeepMind (Apache 2.0).
- **[MLX](https://github.com/ml-explore/mlx)** by Apple and **[mlx-vlm](https://github.com/Blaizzy/mlx-vlm)** by Blaizzy.
- **SQLite FTS5** for exact-word search.

Notion Search is not affiliated with Notion Labs, Google or Apple. Notion is a trademark of Notion Labs, Inc.
