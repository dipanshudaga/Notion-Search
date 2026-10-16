# Contributing to Notion Search

Thanks for your interest. This is a small project with one maintainer, so a few notes up front save everyone time.

## Open an issue first

Please open an issue before you write code, for a fix as much as for a feature. You get a clear "yes", "not now" or
"here is how it should fit" before you spend time on it. A pull request without an issue may be closed with a link
back here.

Good places to help:

- Ports to other platforms, such as Windows or Linux. The model code (`embedder.py`) is the part tied to Apple Silicon.
- Better code: simpler, faster, or easier to read.
- Search quality: ranking, snippets, and things Notion Search misses.

## Set up

```bash
git clone https://github.com/dipanshudaga/Notion-Search && cd Notion-Search
uv run python -m notion_search.engine "your query"   # search from the terminal
uv run notion-search status
```

You need a Mac with Apple Silicon and Notion Desktop to run it. The code is small: `cli.py` (menu and installer),
`service.py` (background service), `engine.py` (search and ranking), `indexer.py` (reads Notion's database) and
`embedder.py` (the model). The README has a diagram.

Stop the installed background service before you test your own copy, or the two will compete for the same Notion tab:

```bash
launchctl bootout gui/$(id -u)/com.notionsearch.service
```

Bring it back with `notion-search restart` when you finish.

## Pull requests

Once an issue says go:

- Keep it to one fix or one feature, with the reason in the description.
- Match the style of the code around it.
- Say how you tested it. There is no automated test suite yet, and adding one is welcome.
- For a change to ranking, show a few queries before and after.
- For speed or memory changes, measure before and after on the same Mac.

Everything stays on the user's Mac, so changes that send data elsewhere, such as telemetry or cloud services, are not
a fit.

## What to expect

This is maintained next to other work, so replies can take a few days. A quiet week means busy, not ignored. Not every
pull request will be merged, even good ones, because the project has a direction.

## Reporting a bug

Please include your Mac (chip and memory), the macOS, Notion and Notion Search versions, what you did or searched for,
and what you expected. Logs are in `~/Library/Logs/NotionSearch.log` (or run `notion-search logs`). They can contain
your search terms and page titles, so read them before you paste.

For a security problem, use "Report a vulnerability" on the Security tab instead of a public issue.
