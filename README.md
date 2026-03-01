# TorBox E2E - Torrent Seed & Download via TorBox API

A Python CLI tool that creates torrents from local files, seeds them, uploads to [TorBox](https://torbox.app) via their API, and downloads them back. Supports both single files and directories with individual file downloads.

## Setup

```bash
python3 -m venv venv
./venv/bin/pip install libtorrent requests
```

## Usage

### Test mode — create a dummy file, seed it, round-trip through TorBox

```bash
./venv/bin/python3 torbox_e2e.py --api-key <YOUR_KEY> --size-mb 100 -v
```

### Seed a directory to TorBox

```bash
# Seed and upload to TorBox (all files become one torrent):
./venv/bin/python3 torbox_e2e.py --api-key <YOUR_KEY> \
  --source-dir /path/to/files -v

# Just seed locally (don't upload yet), save the .torrent file:
./venv/bin/python3 torbox_e2e.py --api-key <YOUR_KEY> \
  --source-dir /path/to/files --seed-only --torrent-out ./my.torrent -v
```

### Download files from TorBox

```bash
# List files in a completed torrent:
./venv/bin/python3 torbox_e2e.py --api-key <YOUR_KEY> --list-files <TORRENT_ID>

# Download a specific file:
./venv/bin/python3 torbox_e2e.py --api-key <YOUR_KEY> --download <TORRENT_ID> --file-id <FILE_ID>

# Download everything as a zip:
./venv/bin/python3 torbox_e2e.py --api-key <YOUR_KEY> --download <TORRENT_ID> --zip
```

### All options

```
--api-key KEY        TorBox API key (or set TORBOX_API_KEY env var)
--source-dir DIR     Seed an existing directory to TorBox
--list-files ID      List files in a completed torrent
--download ID        Download from a completed torrent
--file-id ID         Download a specific file by ID
--zip                Download as zip
--size-mb N          Dummy zip size for test mode (default: 3)
--port N             Seeding port (default: 6881)
--seed-only          Only seed, don't submit to TorBox
--allow-zip          Allow TorBox to zip files (default: individual files)
--name NAME          Custom torrent name
--torrent-out PATH   Save .torrent file to this path
--poll-interval N    Seconds between polls (default: 10)
--poll-timeout N     Max wait in seconds (default: 7200)
--output-dir DIR     Where to save downloads (default: .)
--keep-temp          Don't delete temp files
-v, --verbose        Debug logging
```

## How it works

1. Creates a `.torrent` file (v1 format) with public trackers + DHT
2. Seeds via libtorrent on port 6881
3. Uploads the `.torrent` to TorBox API (`POST /v1/api/torrents/createtorrent`)
4. Polls TorBox (`GET /v1/api/torrents/mylist`) until download completes
5. Retrieves files via TorBox CDN (`GET /v1/api/torrents/requestdl`)

## Requirements

- Python 3.10+
- Machine accessible from the internet (DMZ, port forwarding, or public IP) for seeding
- A [TorBox](https://torbox.app) account and API key
