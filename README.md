# Notion Search

Notion Search makes the search box in Notion Desktop faster. It finds pages by exact words and by meaning. It works offline. Your notes stay on your Mac.

## What you get

- You press **Cmd+K** in Notion, as before. The results come from your Mac, not from the internet.
- A search takes about 0.03 seconds.
- The search reads all the text on all your pages. This includes long pages and table cells.
- You can search by meaning. For example, "what to pack for a trip" finds your packing list.
- The search finds new and changed pages by itself. You do not run any command to update it.

## What you need

- A Mac with an Apple Silicon chip (M1, M2, M3 or newer).
- Notion Desktop, in the Applications folder.
- About 1 GB of free disk space.

The install command checks the rest. It installs Python 3.12 if Homebrew is on your Mac. If Git is missing, it opens the window for Apple's command line tools. This is a small download. You do not need the full Xcode app.

## How to install

1. Open the **Terminal** app. Press Cmd+Space, type "Terminal", and press Enter.
2. Copy this command into Terminal. Press Enter.

```bash
curl -fsSL https://raw.githubusercontent.com/dipanshudaga/Notion-Search/main/install.sh | bash
```

3. Wait. The first install takes 5 to 10 minutes. It installs the tools, downloads the language model (about 300 MB), and reads your pages.
4. When you see "Background service started", the install is complete.

If the command stops and asks for Git or Python, install what it names. Then run the same command again.

The language model comes from Hugging Face. If Terminal says the download failed, do these steps:

1. Open [huggingface.co/google/embeddinggemma-2](https://huggingface.co/google/embeddinggemma-2). Accept the licence.
2. Run `~/.notion-search/.venv/bin/hf auth login` in Terminal. Follow the steps on the screen.
3. Run the install command again.

## How to use

1. Open Notion as usual.
2. Press **Cmd+K**.
3. Type your search.

**Notion restarts one time.** Notion Search needs Notion to start with a special setting. If you open Notion without this setting, Notion Search closes Notion and opens it again with the setting. This takes about 5 seconds. It happens each time you open Notion from the Dock or from Spotlight. Your pages are safe. Notion saves them all the time.

## The control menu

Run this command in Terminal:

```bash
notion-search
```

Use the arrow keys to move. Press Enter to choose. Press `q` to leave.

| Option | What it does |
|---|---|
| Update | Installs the service again and refreshes the tools. |
| Status | Shows if the service runs and if it has a link to Notion. |
| Restart | Restarts the background service. |
| Reindex | Reads all your pages again from the start. |
| Uninstall | Removes the service. |
| Quit | Closes the menu. |

If the command `notion-search` is not found, run `~/.notion-search/notion-search`.

## If something is wrong

- **Search is not fast.** Run `notion-search` and choose **Status**. The service must show "running".
- **A new page does not show in the results.** Wait 15 seconds. Notion Desktop must have the page on your Mac. Open the page one time in Notion Desktop.
- **The results look old or wrong.** Choose **Reindex** in the menu. This takes 2 to 3 minutes.
- **You want to see the log.** Run `notion-search logs`. Press Ctrl+C to stop.

## How to remove it

1. Run `notion-search`.
2. Choose **Uninstall**.
3. Choose **Service** to keep the index. Choose **Everything** to remove the index and the tools also.

Notion itself stays the same. After you uninstall, Notion works as it did before.

## Privacy

- All work happens on your Mac. Notion Search sends no data to any server.
- The only download is the language model, one time, from Hugging Face.
- Notion Search reads the Notion database file on your Mac in read-only mode. It never writes to this file.
- Notion Search does not change the Notion app. Notion keeps its official signature.

Port 9222 on your Mac connects Notion Search to Notion. Other programs on your Mac can use this port also. Do not use Notion Search on a Mac that other people control.

## How it works

Notion Search has three parts.

1. **Reader.** It reads the pages in the Notion database file on your Mac. It cuts long pages into parts of about 1,500 characters.
2. **Index.** It keeps two lists. One list holds the words. The other list holds the meaning of each part, as numbers from the model.
3. **Connector.** When you type in Cmd+K, Notion asks its server for results. Notion Search catches this request. It gives Notion the results from your Mac. Notion shows them as usual.

The files:

| File | Job |
|---|---|
| `notion-search` | The menu and the install commands. |
| `notion_service.py` | The background service. It connects to Notion and keeps the index current. |
| `search_engine.py` | The search. It joins the word results and the meaning results. |
| `notion_indexer.py` | Reads the Notion database and builds the index. |
| `embedder.py` | Runs the language model on the graphics chip of the Mac. |

The index is in `~/Library/Application Support/NotionSearch`. The log is in `~/Library/Logs/NotionSearch.log`.

## Credits

Notion Search uses ideas and tools from other people. Thank you to all of them.

- **[DigUp](https://github.com/ARahim3/DigUp)** by ARahim3 (MIT licence). DigUp searches the files on your Mac with the same model. We learned from it in these ways:
  - The exact text prompts for the model (`task: search result | query: ...`).
  - Scoring a result against all other results for the same search, instead of a fixed cut-off.
  - Storing a "fingerprint" of the model in the index. A change of model then rebuilds the index by itself.
  - Running the model on one thread only.
  - The tested version of `mlx-vlm` that supports the model.
- **[EmbeddingGemma 2](https://huggingface.co/google/embeddinggemma-2)** by Google DeepMind (Apache 2.0). This is the model that finds results by meaning.
- **[MLX](https://github.com/ml-explore/mlx)** by Apple and **[mlx-vlm](https://github.com/Blaizzy/mlx-vlm)** by Blaizzy. They run the model on the graphics chip of the Mac.
- **SQLite FTS5** finds results by exact words.

Notion Search is not made by, or connected to, Notion Labs, Google or Apple. Notion is a trademark of Notion Labs, Inc.
