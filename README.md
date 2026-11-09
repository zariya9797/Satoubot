<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:7b7b730121a5c424183b9b660d9c9e6aee8232df3ef390de029e51b0531709f1 -->

# SatouBot

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:32d899aa2ddc20edd225eb482fdfbbf05d4c4b99d7c75c30af95f3dbf39dc021 -->

## Automatic startup with systemd (WSL)

The Telegram local Bot API must already have a working `satoubot-api.service`; the installer retains its current settings and backs up any existing `satoubot.service` before replacing it. Verify that the API unit's working directory and credentials match your existing Telegram API setup. The bot unit uses this checkout and `.env` and waits up to 90 seconds for the configured `BOT_API_URL` TCP port. A listening port does not prove Telegram authentication or later API requests will succeed. The API dependency is for your local Telegram Bot API, not an external catalogue.

After active downloads/uploads finish, stop any manually running SatouBot with Ctrl+C. In your bot checkout:

```bash
sudo bash scripts/install-systemd.sh
sudo systemctl enable satoubot.service
sudo systemctl restart satoubot.service
systemctl is-active satoubot-api.service satoubot.service
```

`restart` starts the API dependency if needed. Verify both units are active and then send `/start` to the bot. The installer does not start or restart either service itself. Use `journalctl -u satoubot.service -n 50 --no-pager` and `journalctl -u satoubot-api.service -n 50 --no-pager` if startup fails. Review logs for secrets before sharing. Start one bot process for a token: once the service is running, do not separately start `python -m satoubot.app`.

For WSL, systemd must be enabled in `/etc/wsl.conf` and the distro must be running. Enabling a systemd service starts it when the WSL distro starts; it does not itself start the distro on Windows login. Wait for active transfers before restarting the bot service.

## Local media and torrent downloads

<!-- /coderabbit-ai-generated:v1 -->

### Install and update on WSL / Ubuntu

After active downloads/uploads finish, stop the manually running bot with Ctrl+C, then:

```bash
cd ~/Satoubot-clean
git pull --ff-only origin coderabbit/fix-animepahe-cookie-refresh/4d4b1461
sudo apt-get update
sudo apt-get install -y ffmpeg aria2
source .venv/bin/activate
python -m satoubot.app
```

Continue running your existing Telegram local Bot API service (`satoubot-api.service`) when `BOT_API_URL` points to it. The removed browser API, Brave debugger, Docker containers for that API, and WSL socat relay are no longer used by SatouBot. Its old `.env` API setting is ignored. Do not run both a manual bot process and `satoubot.service` for the same token. Existing Python requirements are unchanged.

### Settings and file review

Open **Settings** (or `/settings`) to save an output format and delivery mode for your admin account in the existing local SQLite database. Changes affect new selections. Choose **Original**, **MKV (keep codecs)**, **MP4 (keep codecs)**, or **MP4 (H.264/AAC)**. Remuxing changes the container without re-encoding. H.264/AAC MP4 re-encodes video/audio and converts compatible text subtitles. Some codecs, image subtitles, attachments, and data tracks cannot go into every container; failed conversions retain the original and report the failure. Arbitrary FFmpeg arguments are not accepted.

Choose a file from your catalogues or send an HTTP(S) file URL. The confirmation shows the source, format, output choice, and audio/subtitle information. Direct media track checks read at most 8 MiB in bounded public HTTP ranges into a local sample; FFprobe never opens a remote URL. Samples can be incomplete, and some providers do not expose tracks until the full file is downloaded. Add-on language/subtitle declarations are labelled as reported metadata. External subtitle links are listed as declarations and are not automatically fetched or embedded.

HLS downloads still support finite, unencrypted TS/fMP4 playlists with embedded audio. Set output to **MKV** to remux the downloaded TS file into MKV. Separate audio/subtitle playlists, DRM, live streams, byte ranges, and discontinuities remain unsupported; no protection is bypassed. Completed local files are inspected again for embedded audio/subtitle languages and codecs.

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:403ff218718b75358bf5290116efa0605f178202ead0c3eb2ec844ff7425990e -->

Choose **Download only** to retain the file on this server. For direct files at or below 2 GB, **Transfer** uses the saved delivery default and **Download only** overrides it. Direct files above 2 GB and up to 10 GB offer **Download only** or **Give link**. Files at or below 2 GB can later enter Telegram review from **Saved files / links → Review Telegram upload**. **Convert format** is available from local files and upload review. The original is retained when producing a converted file. Existing output files are never overwritten. A fresh preview and individual approval are required for every Telegram upload. Downloads and conversions are limited to 10 GB; conversion can need space for both original and output.

<!-- /coderabbit-ai-generated:v1 -->

### Torrent files

Send a magnet link, a public HTTP(S) `.torrent` URL, or a `.torrent` document under 4 MB. Compatible add-on `infoHash`/magnet results also open torrent selection. Magnets fetch metadata first. Choose one file in the paginated list, then explicitly choose **Download only** or **Download → review upload**. No content download starts until that confirmation; empty files and files above 10 GB cannot be selected. Torrent metadata normally has filenames/sizes, not verified language tracks; embedded tracks are inspected after completion.

The aria2 process is owned by the current task and uses DHT/peer exchange for public v1/hybrid torrents (piece size up to 16 MiB). Trackers, web seeds, external metadata URLs, and private tracker credentials are discarded. Private torrents and v2-only torrents are unsupported. Availability depends on reachable peers and WSL/network configuration. Torrent mode intentionally opens peer-to-peer networking; the HTTP downloader retains its separate public-destination checks. Normal BitTorrent piece sharing can occur during download, capped at 64 KiB/s; continued seeding is disabled after completion. No RPC server is exposed. Selecting one file can also require adjacent pieces from another file; the task disk limit includes a bounded allowance for those pieces. Status reports approximate disk bytes written. Cancellation terminates that aria2 process and removes incomplete content for that task.

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:7cbfacfec1e8f1fd7363352e85a8ced9318eaa29ffb05a7cd81a273015a0ff2a -->

**Back** returns to the previous screen. **Cancel** or `/cancel` lists active work and lets you cancel one or all tasks; **Active transfers** has the same controls. `/stop` cancels active work and turns transfers and Daily Uploads off; `/start` opens the menu and re-enables transfers after `/stop`, while Daily Uploads must be started separately. A cancelled upload may already be in Telegram: check the destination before retrying. Settings remain local; Telegram is the only upload destination.

<!-- /coderabbit-ai-generated:v1 -->

The implementation was informed by the settings, selection and local-download flows in [mirror-leech-telegram-bot](https://github.com/anasty17/mirror-leech-telegram-bot). It uses SatouBot's existing modules and approval model. Existing group, daily-upload and migration features are retained; no new external upload service or catalogue was added.


<!-- /coderabbit-ai-generated:v1 -->

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

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:8bfb7e462b9443addf43cb5b3b1aceeae36f671987ea77f720a8a0ca2ff51638 -->

A direct HTTP download uses up to four verified byte-range connections when the source supports them. Torrent downloads use aria2. FFmpeg/FFprobe inspect local media and apply your selected output format; see **Local media and torrent downloads** below.

<!-- /coderabbit-ai-generated:v1 -->

Supported HLS downloads accept finite, unencrypted MPEG-TS and fragmented MP4 playlists, including master playlists with embedded audio. Choose **Transfer** after reviewing the unknown size and 10 GB local limit, or **Give link** to use the original playlist in VLC or another app. The bot checks each destination and redirect, downloads segments without transcoding, and removes partial files if the download fails or exceeds the limit. Live, encrypted, byte-range and separate audio/subtitle playlists need manual handling. A completed file above 2 GB remains on the PC; smaller files enter the normal upload review. HLS with unknown size is excluded from Daily Uploads.

<!-- /coderabbit-ai-generated:v1 -->

<!-- /coderabbit-ai-generated:v1 --> Otherwise it uses a single connection. Before saving and before downloading, the bot checks sources using a small byte range, validates public redirect destinations and reports unavailable, blocked, temporary and manual-only sources. <!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:675c83f5183d875588f3d81796be3aa30c1a10896f4dcfcf12d1d4bf605fd0e8 -->

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:a506f5920b32e3efc6eb80b2c175efdfc089eb0201376485ed363211dd5ae582 -->

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:9a48d70679783e1fea8457cd73f20092f3c455bf60d5dc5a283cfb2ac729692a -->

Direct files must still pass the initial small-range check. If a signed GET link rejects HEAD, the bot checks an exact one-byte range and the expected size before downloading. If a server later ignores parallel ranges while returning the same strong validator, the bot retries with one connection and verifies that validator and the exact size. A changed or missing validator stops the retry.

<!-- /coderabbit-ai-generated:v1 -->

<!-- /coderabbit-ai-generated:v1 -->

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

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:22444b2b98b2fbf4c6319ce3ff37b0ec139aa707ce526b600302b63998355c5a -->

When the bot starts, it sends “I'm Back” to the owner if Telegram permits messages to that chat. Send `/start` to remove the old bottom keyboard and open the inline menu.

<!-- /coderabbit-ai-generated:v1 --> Send a movie title in the private chat to search. Use **Admin management** to list, add or remove numeric Telegram user IDs; only the configured owner can change admins, and the list is stored in the database. Each admin must start a private chat with the bot.

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:6c27e94bd3cbfcb8417e853e89af7c062568d3bbc2c5680840d10c7d755e3ead -->

Use **Start transfers** and **Stop transfers** to allow or block new downloads and uploads. The switch persists across restarts and lets already running transfers finish. Up to two downloads and two uploads can run at the same time; conversions use download slots. Additional transfers must wait for a slot. **Active transfers** and `/cancel` list individual tasks and offer cancellation. `/stop` cancels all active work and switches transfers and Daily Uploads off. A cancelled or failed upload may already be present on Telegram: inspect the destination before retrying. Uploads still require individual approval.

<!-- /coderabbit-ai-generated:v1 -->

<!-- /coderabbit-ai-generated:v1 --> <!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:de2ef4aa517506ef486208025498897c72c699345fbdfbeeb4d9425d402ff666 -->

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:f26e2540ad2c961c498434d95274572fd001fe7026ac2251093998f39d287ea9 -->

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:e26f8c6d35d64d3192c8c0f018e56715f10cbe75a2d43060f45230f481e64484 -->

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:d3959e2ac821b81badff5e1cf986cb842cc0698f23ed2fe46a7b167475bbaff4 -->

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:3ce28051ec55b804fb98cca91dbb9e63509c6827429e9172d68245dabe198c06 -->

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:f362dec3c7caa0fd73ba2a9a73d93311fa8cd60c1e7af945d1e1a2616d4f69bf -->

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:64590d565ac56e34e169dd44c413dbcc8d88dd8fd37054205e0688a1b799c688 -->

Each command or menu navigation sends a fresh menu at the bottom of the chat and removes the preceding menu where Telegram permits. Old menus and approval previews cannot be used after a newer screen replaces them. A new command or text message cancels that admin’s earlier temporary searches and file checks. `/cancel` offers individual and all-task cancellation; `/stop` cancels all work, including downloads and uploads. `/start` opens the menu and re-enables ordinary transfers after `/stop`. While navigating, pending checks and transfer status appear on the current menu, and search results load there or appear as a Results button. Background progress may update the current menu; download and upload progress messages are separate.

<!-- /coderabbit-ai-generated:v1 -->

<!-- /coderabbit-ai-generated:v1 -->

<!-- /coderabbit-ai-generated:v1 -->

<!-- /coderabbit-ai-generated:v1 -->

<!-- /coderabbit-ai-generated:v1 --> <!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:8b97c2928c19d25b4622281c5c2c13ece1a56ac627385af84906690977012118 -->

Saved links are paginated, and Upload approvals opens a compact list before showing the selected preview. Back restores the previous screen and its input prompt, including a movie file list. After five minutes without interaction (checked every 15 seconds), temporary menu messages are deleted; active work delays cleanup. Transfer progress, upload reviews and outcomes remain separate. Telegram deletion failures fall back to removing the menu buttons. Cleanup tracks messages created during the current bot process; it does not scan or delete your chat history. Use /start to reopen the menu. A pasted manifest.json URL, or a manifest document with its original manifest URL in the caption, is saved as an add-on. A stream-only add-on still needs a searchable catalogue for movie names.

<!-- /coderabbit-ai-generated:v1 -->

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

<!-- coderabbit-ai-generated-section:v1:1f397e57e3eb26837769c17e997ff8ac2d8b77b1a2fe8dbe75fe4977f3aa7e4c -->

### Add-on sources

Only manifests you explicitly saved are queried. No extra catalogue or provider repository is installed automatically. Open **Add-ons → Catalogues / artwork sources** to see each saved catalogue, its media types, and whether it is enabled. Movie cards prefer `poster` over `background`, identify the metadata/artwork source, and fall back to a background only when no poster is supplied. Existing downloaded files and local settings are retained when upgrading; obsolete built-in API manifests and cached results are removed.



<!-- /coderabbit-ai-generated:v1 --><!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:7d7e571d562c0f9b0b02992c051280822b2b9cff5d094affb096728e84ce8dde -->

Availability depends on the selected catalogue supplying episode metadata and a source supporting that title. Downloads remain individually confirmed, with up to two downloads and two uploads concurrently; automatic whole-season queues are not implemented.

<!-- /coderabbit-ai-generated:v1 -->

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

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:24dbffeeaff85135c2a25b11446e5756eae7113726d9c8e741f00ffd6e9853c4 -->

Daily selections, skipped/no-file titles, title IDs and conservative title/year duplicate checks persist. Daily Uploads offers **Cancel one** and **Cancel all daily**. Cancelling one daily item skips it and permits the next; an upload whose outcome is uncertain is marked for attention and is never automatically retried. Cancel all daily stops the daily cycle without cancelling unrelated manual transfers. A catalogue scan with no candidates waits ten minutes. At the daily quota the bot switches Transfers off; explicitly start another cycle later. Counts use UTC, and each Telegram upload still needs approval.

<!-- /coderabbit-ai-generated:v1 -->

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

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:b72e8a0edff6364e3d831d87b9b69b4c9ec8865fece4dbaafb8f2ae7ce3f1c31 -->

Inline retrieval of already published Telegram files, migration, source refresh, DASH, encrypted and live HLS downloads, generic download-page extraction, automated error retries and channel availability monitoring are not implemented yet.

<!-- /coderabbit-ai-generated:v1 -->

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
<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:9304663bb092ebef8e613278569cac528f89457713a3cc82df6e257804713427 -->

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:63872f37b5f04c82a83d0dc2915f90f70026d19198608378b288555ca496923b -->

3. **Add-ons** offers two built-in choices: **Add Yoru providers** and **Add HDHub**. Select one to save it; no preset is installed automatically. Add any other Stremio or Nuvio manifest yourself. Use each ON/OFF row and, for a provider repository, **Providers** to manage individual providers. Your individual choice persists, including when a provider was disabled by default in the manifest. Turning the repository OFF pauses all its providers while retaining their individual choices. Switches apply to new lookups.

The status icon is separate from ON/OFF: 🟢 means the manifest was reachable; 🔴 reports an unavailable or invalid manifest, a failed search/file lookup, or no results from the last lookup; ⚪ means OFF or not yet checked. A source can return no results for one title while working for another. The row explains the reason; a stream-only add-on also says it has no title search. Manifest reachability does not verify every provider or media link. Status reports expire after 15 minutes, so old results return to unverified. Opening Add-ons checks enabled visible manifests in the background (at most four at a time, with an eight-second limit each). OFF sources are not checked automatically; **Check** or **Check manifests on this page** can check them explicitly without switching them ON. **Refresh manifest** checks and updates that saved manifest while preserving your ON/OFF choices; a failed refresh retains the previous configuration. An add-on may still need its own setup or be temporarily blocked.

Only saved manifests are queried for searches. Manifests and switch choices persist in the local database. Provider scripts are fetched when looking up streams.

<!-- /coderabbit-ai-generated:v1 -->

<!-- /coderabbit-ai-generated:v1 -->
<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:8619b6da6c26c4ffaea9d24c3732550a9255337a3f2ce19ac84fd333b501d1d2 -->

4. Send a movie name or `/search Insidious`. Select the correct movie; the bot checks sources concurrently and shows checked files as they become available, with landscape artwork where available. <!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:9e3b091aa8baff5c9ac890ae34ed8e48e39a1d6d77e979bef7ef0390c8d5510a -->

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:fd21c95820dec32e9f3d7696b33727812b8512bd051fc150ce377b8d0c0118f6 -->

Source lookup uses at most four concurrent providers, a 30-second timeout per provider, and a 90-second overall timeout.

<!-- /coderabbit-ai-generated:v1 -->

<!-- /coderabbit-ai-generated:v1 --> Add-ons on the same host are queried one at a time. Manifest and movie metadata responses are cached in memory for 60 seconds; file availability and access are still checked before downloads. A size labelled unknown is rechecked before download. Search alone never downloads or uploads media.

<!-- /coderabbit-ai-generated:v1 -->
5. <!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:f170a0d2386266b1c93feccf8ae6db1313ece5396e76a4191a369342198d36de -->

File choices show four per page with More/Previous and Cancel. <!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:55c2434624d2efa149d30aa6a66e410355cd2feb41cb258d0bc0625777e59281 -->

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:446f4e0e21ea0722a5437f85ee541289730f103d42f5b2247021fc39bc7a65e6 -->

Select a **DW** or **HLS** button to review the source. Choose **Transfer** or **Download only** to start a download after approval, or **Give link** to use the source manually. Direct files above 2 GB offer **Download only** and **Give link**.

<!-- /coderabbit-ai-generated:v1 -->

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
