<!-- coderabbit-ai-generated:v1 -->

# SatouBot

Early foundation for a private Telegram media administrator. This is not yet the complete bot.

## Current functionality

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:87948aaa984bf25a9e6ce9d14879ae6030f10981f89d51340fc7beba15996bf7 -->

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:cd08f449df7a869c6b60d71cde3044f59fd70464fd10e392552c1f10ad0ecdac -->

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:e48e272575286e378c328bafee4745d4930f8ea4ce0609ae185c6e012d4aa92d -->

The owner and authorized admins can send an HTTP(S) media URL or choose a search result, approve a download up to 10 GB on local storage, and review files at or below 2 GB for Telegram upload.

<!-- /coderabbit-ai-generated:v1 -->

<!-- /coderabbit-ai-generated:v1 --> On a review preview, choose **Set thumbnail** and send a photo; review the new preview before approving. Telegram may ignore custom thumbnails for some file types or media without recognizable video dimensions. The upload uses the logged-in Telegram user account through MTProto, so the post is sent by that account. The selected account needs access to the destination. File transfers report progress, speed, and estimated time. `/status` shows CPU and memory, `/find` searches locally recorded published items, and `/saved` lists oversized links.

<!-- /coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:2adf38604fc3a0c26566982025949205956355cc4286313fa64c92dec101169b -->

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:007bc17cda87143c84e1510467b74f3ab77596cf5107e8832ece0ddbd6f4737d -->

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:750b9412dc59dc775f8a57371e759409a669f0bb65f5e7f9018457286e871327 -->

A download uses up to four HTTP byte-range connections when the server advertises range support and provides a stable validator. No aria2 integration or transcoding is used; the verified filename supplies the displayed format, and ffprobe is not required.

Supported HLS downloads accept finite, unencrypted MPEG-TS and fragmented MP4 playlists, including master playlists with embedded audio. Choose **Transfer** after reviewing the unknown size and 10 GB local limit, or **Give link** to use the original playlist in VLC or another app. The bot checks each destination and redirect, downloads segments without transcoding, and removes partial files if the download fails or exceeds the limit. Live, encrypted, byte-range and separate audio/subtitle playlists need manual handling. A completed file above 2 GB remains on the PC; smaller files enter the normal upload review. HLS with unknown size is excluded from Daily Uploads.

<!-- /coderabbit-ai-generated:v1 -->

<!-- /coderabbit-ai-generated:v1 --> Otherwise it uses a single connection. Before saving and before downloading, the bot checks sources using a small byte range, validates public redirect destinations and reports unavailable, blocked, temporary and manual-only sources. <!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:675c83f5183d875588f3d81796be3aa30c1a10896f4dcfcf12d1d4bf605fd0e8 -->

For direct files, it stops if a source ignores the range.

<!-- /coderabbit-ai-generated:v1 --> <!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:3e3aa1bbfff2126714db28d4643dcea1d8c4b8c805f73a18af8bb0b78ef53304 -->

Direct files require a verified size. Supported HLS playlists may be approved with an unknown size and stop at the 10 GB local limit.

<!-- /coderabbit-ai-generated:v1 --> Download and upload progress messages show a progress bar, size, speed and ETA. Speed depends on the source and network. <!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:5c6486be6df86ece30017036307bec0b5962e0051a14f003b07fa5197b195b4f -->

The MTProto uploader uses Telethon's standard uploader by default. An experimental option sends several file parts concurrently through the same user session; see **Experimental upload speed** below.

<!-- /coderabbit-ai-generated:v1 -->

<!-- /coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:049204d5e1a86d67e82eff8a07b157d70779dcc55fdbf06bff1f16d8fe287ff8 -->

## Transfer controls and menu

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:c1e59b9411bb3c153bf6f6bab780accf4529ece171cfb3015681456d2efe2204 -->

Send `/start` to remove the old bottom keyboard and open the inline menu. Send a movie title in the private chat to search. Use **Admin management** to list, add or remove numeric Telegram user IDs; only the configured owner can change admins, and the list is stored in the database. Each admin must start a private chat with the bot.

Use **Start transfers** and **Stop transfers** to allow or block new downloads and uploads. The setting persists across restarts. One transfer runs at a time, and menus and searches remain usable during it. **Active transfers** shows and cancels the current transfer. A cancelled or failed upload may already be present on Telegram: inspect the destination before retrying. A running transfer finishes when transfers are switched off. Uploads still require individual approval.

<!-- /coderabbit-ai-generated:v1 --> <!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:de2ef4aa517506ef486208025498897c72c699345fbdfbeeb4d9425d402ff666 -->

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:f26e2540ad2c961c498434d95274572fd001fe7026ac2251093998f39d287ea9 -->

Menus reuse one temporary panel per admin. Pending searches, file checks and transfer status appear on the current panel as you navigate; search results load there or appear as a Results button if you have opened another menu. Saved links are paginated, and Upload approvals opens a compact list before showing the selected preview. Back restores the previous screen and its input prompt. After five minutes without interaction (checked every 15 seconds), temporary menu messages are deleted; active work delays cleanup. Movie cards, file confirmations, selected-destination notices and transfer outcomes are preserved. Telegram deletion failures fall back to removing the menu buttons. Cleanup tracks messages created during the current bot process; it does not scan or delete your chat history. Use /start to reopen the menu.

Migration is available from the menu and /migration; copying channel/group messages still awaits the source and destination workflow. Group management remains a placeholder. My channels lists channels and groups owned by the logged-in user where the bot is an administrator.

<!-- /coderabbit-ai-generated:v1 --> When no destination is set, upload review opens this selection; selecting a destination generates a fresh approval preview. Bot administrator/posting access is checked again before upload.

<!-- /coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:7ad39605ef842149745c3b50079680e3468b5c871a091a39f41db02efaaf0e23 -->

Add-ons lists every saved catalogue and provider repository with ON/OFF buttons. Provider repositories also expose individual provider switches. Choices persist across restarts and apply to new lookups; already running requests can finish. After the first explicit toggle, missing default repositories are no longer automatically registered, preserving your selected combination. Disabled sources remain listed and can be enabled again. Use Add manifest to import another source.

Search covers the search-capable catalogue types declared by enabled sources, including series and anime catalogues. Series navigation is title → season → episode → available files. Seasons and episodes sort numerically, specials use season 0, and episode filenames include S01E02. <!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:ba61dbf71a4d391fbb6986ec670ca2dc9d2ce9749cb573a87505ba6e314b304e -->

Stream add-ons receive the catalogue episode ID only when their manifest resource type and ID-prefix rules match. Compatible enabled metadata add-ons can supply details when the originating catalogue cannot. Embedded movie/episode file links are used directly when present, retaining request headers and the normal access probes. Nuvio providers receive the parent title TMDB ID with `movie` or `tv` and season/episode numbers, as described in the [Nuvio provider interface](https://github.com/yoruix/nuvio-providers/blob/main/DOCUMENTATION.md). <!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:867ef1c26c2e4208bdca8e27891d80b36255b495691c1157aaece72adaa00ab0 -->

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:aafccc069dcc8671ad48fc45a087b0ede007512e1dfdbf39ac6cc9978acf2e8e -->

The bot gets IDs from your enabled catalogues and requires no IMDb or TMDB API key. If the selected metadata lacks a TMDB ID, it checks compatible enabled metadata add-ons for the same title ID. If none supplies a TMDB ID, plugins that require one cannot fetch that title; stream add-ons can still return results using the catalogue ID. The bot does not guess identities from similar titles.

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:37af6d3f829d88b466306caf3c7cbe6d34520b08cd79c263eda878f28cd661ab -->

### Catalogue sources

Add a Stremio catalogue manifest through **Add-ons** and enable the sources you want to search. Movie and series metadata, including episode IDs, come from those enabled catalogues. No separate anime API is required.

<!-- /coderabbit-ai-generated:v1 -->


<!-- /coderabbit-ai-generated:v1 -->

<!-- /coderabbit-ai-generated:v1 -->

<!-- /coderabbit-ai-generated:v1 --> Availability depends on the selected catalogue supplying episode metadata and a source supporting that title. Downloads remain individually confirmed, one transfer at a time; automatic whole-season queues are not implemented.

Download, upload, search, group management, migration and user session code are in separate modules.

<!-- /coderabbit-ai-generated:v1 --> The bot session is managed by the Telegram application; the user session is stored locally. <!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:cf34a90fefbf7919e4ed1107ac01d867678b19c86c155b6e0980465019350997 -->

Movie-name search and automatic catalogue artwork are available through supported Stremio catalogue manifests. <!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:a9909a0ddbb13203dc5ec387e8337f0097a13ca52ec5674628a17fcd553b7f28 -->

Daily Uploads uses the Transfers switch to activate release selection. Open Daily Uploads, choose 4–10 successful uploads per UTC day (default 4), select a channel, and press Start daily transfers. An explicit start activates daily selection on existing installations; it does not start merely because the old transfer setting is already on. The bot browses up to four movie catalogues per enabled add-on, prioritizing recent/new/release/trending catalogue IDs, and sorts titles with catalogue dates from the past 90 days. Availability and release dates depend on the catalogue. This first version selects movies, not automatic episode batches.

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:0962dc29ed937891f932ef4610cde78b84af7b077e59672113307b0726bde8a1 -->

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:0bf29fe49d11a6afbd436455ed5db8f27809c425ec7109a0b9d781c2b7a0237e -->

One release is offered at a time. Only verified files strictly below 2,000,000,000 bytes appear in Daily Upload choices.

<!-- /coderabbit-ai-generated:v1 -->

<!-- /coderabbit-ai-generated:v1 --> Choose a quality/file, press Transfer, then review the name, landscape thumbnail and destination before approving upload. Rename and thumbnail controls remain available before upload starts. Give link returns the source for manual handling and never starts a transfer. Single manual transfers do not contribute to the daily quota.

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:bf898b0397f1592d51a9d435a4fa2962be03b8da2f94724b62d06f0b88d44f61 -->

Daily selections, skipped/no-file titles, title IDs and conservative title/year duplicate checks persist. The next release is checked after upload/skip; a catalogue scan with no candidates waits ten minutes. A failed or uncertain transfer is marked for attention and is never retried automatically. A pending release can be reopened or skipped from Daily Uploads after restart. At the daily quota the bot switches Transfers off; explicitly start another cycle on a later day. Stop transfers prevents new downloads/uploads; already running transfers can finish. Counts use UTC. This is an approval-driven cycle: it cannot publish 4–10 files without your approvals.

### Source history

Open **Source history** to see ready, manual, temporary, and unavailable checks by file host and to turn idle checks on or off. The bot stores these results locally. It uses recent host history to order checked file choices, but every link is checked again before transfer; a previous success does not guarantee a new link works.

After five minutes without user activity, the bot can recheck one randomly selected saved link from an enabled add-on or provider at most once per 15 minutes. Each checked link waits at least 24 hours before another idle check, and longer Retry-After intervals are respected. New user activity cancels an ongoing idle check. Only links discovered with source information after this feature is installed are eligible. The history does not assess catalogue search quality, publish to Telegram, or download files in the background.

<!-- /coderabbit-ai-generated:v1 -->

<!-- /coderabbit-ai-generated:v1 -->

<!-- /coderabbit-ai-generated:v1 -->

## Setup

<!-- /coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:5bc732f79730278c318642c0029ca9f77c19895ebaa59fd8097b948319bde880 -->

### Install on a PC (Ubuntu or Windows with WSL)

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:db7a4107844e90140305ce902e729fd0d3364c8b225d4a29fecdcbb0286996d3 -->

These steps run the bot on Ubuntu. On Windows, first install [WSL with Ubuntu](https://learn.microsoft.com/windows/wsl/install) and run the commands in the Ubuntu terminal. Install Python 3.11 or newer, Git, Node.js 24 or newer and npm first. Check with `python3 --version`, `git --version`, `node --version` and `npm --version`.

Install Bubblewrap with `sudo apt update && sudo apt install bubblewrap`. Nuvio plugins need working unprivileged user and network namespaces; the bot fails closed for plugins if Bubblewrap or those namespaces are unavailable. Catalogue add-on lookups work independently.

<!-- /coderabbit-ai-generated:v1 -->

1. Get this repository and enter its folder. To use this task's changes, first publish the task branch, then check it out:

   ```bash
   git clone https://github.com/cinespicebusinesss/Satoubot.git
   cd Satoubot
   git switch coderabbit/telegram-media-admin-bot/de70788f
   ```

2. Install bot dependencies:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   python -m pip install -r requirements.txt
   npm ci --prefix satoubot/plugin_runtime
   cp .env.example .env
   ```

   On Ubuntu, install the matching `python3-venv` package if creating the environment fails. Keep the terminal open.

3. Create a bot with [BotFather](https://t.me/BotFather). Get your numeric Telegram user ID, and obtain your own API ID and API hash from [my.telegram.org](https://my.telegram.org). Edit `.env` (for example `nano .env`) and set `BOT_TOKEN`, `ADMIN_USER_ID`, `TELEGRAM_API_ID`, and `TELEGRAM_API_HASH`. Leave `BOT_API_URL=http://127.0.0.1:8081` for a server on the same machine. `DATA_DIR=./data` stores downloads in the repository's `data/media` directory. Keep `.env` and the `data` directory private.

4. Install Telegram's **local Bot API server** separately using the [official build instructions generator](https://tdlib.github.io/telegram-bot-api/build.html) for Ubuntu. It requires a compiler, CMake, gperf, OpenSSL and zlib. In a second terminal, from the SatouBot folder, load the credentials and start the installed server:

   ```bash
   set -a
   source .env
   set +a
   telegram-bot-api --local
   ```

   Keep this terminal running. The server uses `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` from the environment and listens on port 8081 by default. If the executable was installed outside your PATH, use its full path. If this bot was previously connected to Telegram's hosted Bot API, follow [Telegram's local-server switching instructions](https://github.com/tdlib/telegram-bot-api#switching) before starting it; moving an existing bot logs it out of the previous API server.

5. In the first terminal, start the bot:

   ```bash
   source .venv/bin/activate
   python -m satoubot.app
   ```

   On first launch enter the Telegram account's phone number, login code, and two-step verification password if requested. The bot uses this user account for uploads. Keep both terminals running; stopping either stops its service. Restart the local server and bot with the same commands after restarting the PC.

6. Add the bot as an administrator to the destination channel or group, with posting permission, and ensure the user account can post there. Open a private chat with the bot and send `/start`. Use **Add-ons** to add a searchable catalogue, such as `https://v3-cinemeta.strem.io/manifest.json`; see **Catalogue and provider search** below. To use inline search, enable it via BotFather's `/setinline`.

This foundation is not configured to start automatically with the PC. A published item with an unknown upload outcome needs manual inspection before retrying.

<!-- /coderabbit-ai-generated:v1 -->

## Work remaining

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:d9d2894ba7cd912ffc037573d9224c035c444c7fc48121c5c8c1206f663dd303 -->

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:bcf4cec059eae3e6359b3e9dc943e80d96947af0988b3bb6a1fd372c18e336fa -->

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:2f6ec2ef61772189d265c825017265380efd5c6d9a26ce38492c8508f018fb3d -->

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:e198d12a2f3c193d663be702e14d7a9f6edd2cfc5edad39072f4766472d23ddb -->

Inline retrieval of already published Telegram files, migration, source refresh, torrent, DASH, encrypted and live HLS downloads, generic download-page extraction, automated error retries and channel availability monitoring are not implemented yet.

<!-- /coderabbit-ai-generated:v1 -->

<!-- /coderabbit-ai-generated:v1 --> <!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:074f3bf4512b0be7380d6e363d7563d026fe34f5a0fac1784ceb8b22e70d3231 -->

Search can list HTTP links returned by providers; unsupported formats and files above 10 GB are saved as links. Direct files require a verified size before download.

<!-- /coderabbit-ai-generated:v1 --> Provider success depends on source availability and compatibility; plugin settings screens are not implemented.

<!-- /coderabbit-ai-generated:v1 -->

<!-- /coderabbit-ai-generated:v1 --> Reported links are recorded but not automatically refreshed. The current search records message IDs and does not deliver a Telegram file. Do not rely on this foundation for unattended publishing or migration.

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:2b768b0766e5fdb057113593cd372e2670be07e86850e9ba5177c61f706d0cb4 -->

## Catalogue and provider search

1. Install Node.js 24+ and run `npm ci --prefix satoubot/plugin_runtime` once, alongside the Python dependencies.
2. <!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:fb37879783d815423664c9757cbb778cdcd0d4db283cf72347493eb253154107 -->

Open **Add-ons** (or `/addons`) and send a Stremio catalogue manifest URL, or upload a JSON manifest and then provide its original URL so relative endpoints can be resolved.

<!-- /coderabbit-ai-generated:v1 --> For example, Cinemeta: `https://v3-cinemeta.strem.io/manifest.json`. The catalogue must advertise a movie catalogue with a `search` extra. For Nuvio plugins, movie metadata must include `moviedb_id`, `tmdb_id`, or a `tmdb:` ID.
3. Add a Nuvio repository URL in the same menu, or use **Add Yoru providers**. The supplied Yoru repository is loaded automatically on the first source lookup if no Nuvio repository has been saved. Manifests persist in the database. Provider scripts are fetched when looking up streams.
<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:8619b6da6c26c4ffaea9d24c3732550a9255337a3f2ce19ac84fd333b501d1d2 -->

4. Send a movie name or `/search Insidious`. Select the correct movie; the bot checks sources concurrently and shows checked files as they become available, with landscape artwork where available. Source lookup uses at most four concurrent providers, a 30-second per-provider timeout and a 90-second overall timeout. Add-ons on the same host are queried one at a time. Manifest and movie metadata responses are cached in memory for 60 seconds; file availability and access are still checked before downloads. A size labelled unknown is rechecked before download. Search alone never downloads or uploads media.

<!-- /coderabbit-ai-generated:v1 -->
5. <!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:f170a0d2386266b1c93feccf8ae6db1313ece5396e76a4191a369342198d36de -->

File choices show four per page with More/Previous and Cancel. <!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:55c2434624d2efa149d30aa6a66e410355cd2feb41cb258d0bc0625777e59281 -->

Select a **DW** or **HLS** button to review the source, then choose Transfer or Give link. Transfer starts a download after approval.

<!-- /coderabbit-ai-generated:v1 --> A separate approval is required before upload. <!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:a4b80ff4a275e67e2e9fe0e75895286cfdfa6f03f908b7da79c882b362f339ee -->

Downloads show the filename, bytes downloaded, speed and ETA every five seconds, including during stalls, and send a completion notice. HLS total size and ETA are shown as unknown.

<!-- /coderabbit-ai-generated:v1 --> The local file is then renamed using the catalogue title, year, detected quality/codec and language labels. Source headers and alternative URLs are retained. <!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:50c1c26cd82f57ca2be7797c1c77be5adbf5b6c1e15d027a63d2627f9b9d9c6b -->

Approved files above 2 GB and up to 10 GB are downloaded locally and are not offered for Telegram upload; files above 10 GB remain links.

<!-- /coderabbit-ai-generated:v1 -->

<!-- /coderabbit-ai-generated:v1 -->
6. <!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:47886b2bd16ef26c9b0fd1206b19ff93c9818d3878a30600dd0adf62ca036a75 -->

**Library search** and `/find` search previously published records. Send a movie title or use `/search` to search online catalogues. Both work with transfers off.

<!-- /coderabbit-ai-generated:v1 -->

For Telegram inline search, enable inline mode for the bot through BotFather (`/setinline`). Type `@your_bot_username Insidious` in Telegram. Results include a **View available files** button that opens the private bot. Inline results are restricted to the configured admin; <!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:4f23426e260a7d8dd36c8fe123c33368ac37f5cfc727865f06d76e384bb78b7b -->

movie, page and confirmation selections expire after one hour; source URLs and file information are stored persistently in the database and shown in Saved links. Identical URLs are merged; matching movie IDs, verified source filenames and byte sizes merge known mirror choices. Files with different names and no shared content identity cannot be proven identical before download.

<!-- /coderabbit-ai-generated:v1 --> `/search` is the private-chat command.

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:5d5fc0435d3fef8777d721187bf6dec0d15c88451972e3bfdcb33c8a7337f4e7 -->

Nuvio scripts run in a Bubblewrap-isolated Node process with a private network namespace, no host user files, a cleared environment and resource limits. Node permissions and `node:vm` do not provide isolation themselves. Plugin HTTP requests pass through a broker that permits public destinations only, pins DNS results and checks redirects individually. Private network plugin destinations are unavailable. Bubblewrap and working unprivileged namespaces are required; plugin execution fails closed if they are unavailable. Only add repositories you trust. The broker restriction applies to plugin requests; other add-on and media requests use their existing network paths. The compatibility bridge provides fetch, timers, URL utilities, Cheerio, CryptoJS and empty `SCRAPER_SETTINGS`.

<!-- /coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:611db00e8b6213770324c61d70aa7ed403c86b4e5ca47895b0f6508470e28849 -->

## Experimental upload speed

The bot already uploads using your Telegram user account. To try sending several large-file parts concurrently, set `UPLOAD_PARALLEL_PARTS=4` in `.env` and restart the Python bot. Values `2` through `4` enable the experiment for files larger than 10 MiB; `1` (the default) uses the standard uploader. These are concurrent requests on the existing user session, not four separate TCP connections. Smaller files use the standard uploader.

Compare the progress speeds on similarly sized uploads on your own connection. Actual speed depends on the connection and Telegram; no improvement is guaranteed and the experimental mode has not been tested against live Telegram. If it fails or performs worse, set the value back to `1` and restart. If an upload outcome is unknown, check the destination before trying again. The experiment preserves the separate upload approval and does not automatically retry a publication.

## Testing

<!-- /coderabbit-ai-generated:v1 -->

<!-- /coderabbit-ai-generated:v1 -->

Install pytest and pytest-asyncio and run `python -m pytest -q`. Tests do not require Telegram credentials. Live Telegram transfers require a real bot, account and destination.
