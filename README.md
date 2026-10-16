<h1 align="center">Notion Search</h1>

<p align="center"><b>Search Notion by what you mean, not just what you typed.</b><br>
A private, offline replacement for the Cmd+K search in Notion Desktop, powered by Google's
<a href="https://huggingface.co/google/embeddinggemma-2">EmbeddingGemma 2</a>.</p>

<p align="center"><a href="https://pypi.org/project/notion-search/"><img src="https://img.shields.io/pypi/v/notion-search" alt="PyPI version"></a>
<img src="https://img.shields.io/badge/macOS-Apple%20Silicon-black" alt="macOS, Apple Silicon">
<a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="MIT license"></a></p>

```bash
uvx notion-search
```

Notion's own search leans on exact words and titles. It misses text deep inside long pages, links you saved, and
anything you only remember by its meaning. Notion Search keeps the same Cmd+K box and answers it from your own Mac
instead. It indexes every page in your workspace and ranks each query by exact words and by meaning together. Each
result opens at the block that matched, with Notion's blue highlight.

## Install

You need a Mac with Apple Silicon, Notion Desktop in `/Applications`, and [uv](https://docs.astral.sh/uv/getting-started/installation/)
(`brew install uv` works). Then run:

```bash
uvx notion-search
```

The first run takes 5 to 10 minutes and sets up everything: the command, the model (about 900 MB, once), the index
and a background service that starts at every login. Open Notion and press **Cmd+K**. Run `notion-search` any time
for the menu.

## The menu

| Option | What it does |
|---|---|
| Update | Gets the newest release, then refreshes the service and index |
| Status | Shows whether the service is running and linked to Notion |
| Restart | Restarts the background service |
| Reindex | Rebuilds the index from scratch (2 to 3 minutes) |
| Uninstall | Removes the service, index, model and the command |

The same actions work as commands: `notion-search update`, `status`, `restart`, `reindex`, `uninstall`, and `logs`.

## What gets searched

Page titles and folder paths, and the full text of every page: toggles, columns, lists, callouts and table cells.
Long pages are split into chunks of about 1,500 characters, so a match deep inside a page still finds it.

Not searched: database properties (status, tags, dates) and the contents of PDFs, images and other files. Notion
Desktop must have opened a page at least once before it can be indexed. New and edited pages are picked up within
about 15 seconds.

## Private

Everything runs on your Mac. No page text, query or telemetry leaves it, and it works offline. The only download is
the model, once. Notion's database is opened read-only and Notion's app files are never changed.

The service talks to Notion over Chromium's DevTools port (`127.0.0.1:9222`), which any process running as you can
reach. Do not use this on a Mac that other people control.

## How it works

```
Notion Desktop ── Cmd+K ──> /api/v3/search request
        │  (intercepted over the DevTools port)
        ▼
service.py ──> engine.py ──> answer in Notion's own response format
        │                    ├─ SQLite FTS5 index   (exact words)
        │                    └─ vectors             (EmbeddingGemma 2, MLX, 8-bit)
        ▼
indexer.py: reads Notion's local database (read-only),
re-embeds only the pages whose text changed
```

Each query runs both ways and the results are merged. A meaning match only counts when it stands well above the
usual score for that query, so nonsense queries return nothing. Pages that hold all your words, and the page you
have open, get a small boost. Under each result you see the one block that matched best.

Search takes about 20 to 50 ms. Memory is about 800 MB, mostly the model. The first index takes 2 to 3 minutes for
about 800 pages. After that only changed pages are processed.

## FAQ

**Why does Notion restart when I open it?** Notion has to run with a debug port so the service can answer its
searches. If you open it normally, the service restarts it once with the port enabled. Your pages are safe, because
Notion saves continuously.

**Windows or Linux?** Not yet. The model runs on [MLX](https://github.com/ml-explore/mlx), which needs Apple
Silicon. A port would be a welcome contribution; see [CONTRIBUTING.md](CONTRIBUTING.md).

**Does it work in the browser?** No. It needs Notion Desktop for Mac.

**Something is wrong.** Run `notion-search status`, then `notion-search logs`. If results look stale, choose
Reindex in the menu.

**How do I remove it?** Run `notion-search`, choose Uninstall and confirm. `uv` stays, because other tools may use
it.

## Contributing

Issues and pull requests are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) first.

## Credits

Built with ideas from **[DigUp](https://github.com/ARahim3/DigUp)** by ARahim3 (MIT), which searches local files
with the same model: the model card's query and document prompts, scoring a hit against the whole score
distribution for a query instead of a fixed cutoff, and a model fingerprint in the index so a model change
triggers a rebuild.

## License

MIT. See [LICENSE](LICENSE). Notion Search is not affiliated with Notion Labs, Google or Apple. Notion is a
trademark of Notion Labs, Inc.
