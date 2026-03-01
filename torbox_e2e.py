#!/usr/bin/env python3
"""
TorBox End-to-End Torrent Seed & Download

Two modes:
  1. Test mode (default): Creates a dummy zip, seeds it, uploads to TorBox, downloads back.
  2. Directory mode (--source-dir): Seeds an existing directory of files to TorBox.
     Supports downloading individual files or everything as a zip.

Usage:
    # Test mode with dummy data:
    ./venv/bin/python3 torbox_e2e.py --api-key <KEY> --size-mb 100 -v

    # Seed an existing directory:
    ./venv/bin/python3 torbox_e2e.py --api-key <KEY> --source-dir /path/to/files -v

    # Seed directory but only create torrent + seed (don't submit to TorBox yet):
    ./venv/bin/python3 torbox_e2e.py --api-key <KEY> --source-dir /path/to/files --seed-only -v

    # List files in a completed torrent on TorBox:
    ./venv/bin/python3 torbox_e2e.py --api-key <KEY> --list-files <torrent_id>

    # Download a specific file from a torrent on TorBox:
    ./venv/bin/python3 torbox_e2e.py --api-key <KEY> --download <torrent_id> --file-id <file_id> --output-dir ./out
"""

import argparse
import json
import logging
import os
import shutil
import signal
import sys
import tempfile
import time
import zipfile

try:
    import libtorrent as lt
except ImportError:
    print("ERROR: libtorrent not found. Install it with:")
    print("  ./venv/bin/pip install libtorrent")
    sys.exit(1)

import requests

# ─── Constants ────────────────────────────────────────────────────────────────

TORBOX_BASE_URL = "https://api.torbox.app"

PUBLIC_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://tracker.moeking.me:6969/announce",
    "udp://explodie.org:6969/announce",
    "http://tracker.openbittorrent.com:80/announce",
]

LISTEN_PORT = 6881
POLL_INTERVAL = 10
POLL_TIMEOUT = 7200  # 2 hours for large files
DUMMY_SIZE_MB = 3

log = logging.getLogger("torbox_e2e")


# ─── Helper Functions ─────────────────────────────────────────────────────────

def get_public_ip() -> str:
    resp = requests.get("https://api.ipify.org", timeout=10)
    resp.raise_for_status()
    return resp.text.strip()


def create_dummy_zip(output_path: str, size_mb: int = 3) -> str:
    random_data = os.urandom(size_mb * 1024 * 1024)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("random_payload.bin", random_data)
    actual_size = os.path.getsize(output_path)
    log.info(f"Created dummy zip: {output_path} ({actual_size:,} bytes)")
    return output_path


def create_torrent_file(source_path: str, torrent_path: str, trackers: list, piece_size: int = 0) -> tuple:
    """Create a .torrent file from a file or directory.

    Args:
        source_path: Path to a file or directory to include in the torrent.
        torrent_path: Where to write the .torrent file.
        trackers: List of tracker announce URLs.
        piece_size: Piece size in bytes (0 = auto). For large files, use 4MB+ pieces.

    Returns:
        (torrent_path, magnet_uri)
    """
    fs = lt.file_storage()
    # add_files works for both files and directories
    lt.add_files(fs, source_path)

    total_size = fs.total_size()
    num_files = fs.num_files()
    log.info(f"Building torrent: {num_files} files, {total_size:,} bytes total")

    # For large torrents, auto piece size can create too many pieces.
    # Use at least 4MB pieces for anything over 1GB to keep the .torrent small.
    if piece_size == 0 and total_size > 1024 * 1024 * 1024:
        piece_size = 4 * 1024 * 1024  # 4MB pieces
        log.info(f"Large torrent detected, using {piece_size // (1024*1024)}MB piece size")

    t = lt.create_torrent(fs, piece_size=piece_size, flags=lt.create_torrent.v1_only)

    for i, tracker in enumerate(trackers):
        t.add_tracker(tracker, tier=i // 3)

    t.add_node("router.bittorrent.com", 6881)
    t.add_node("router.utorrent.com", 6881)
    t.add_node("dht.transmissionbt.com", 6881)

    t.set_creator(f"libtorrent {lt.__version__}")
    t.set_priv(False)

    # For directories, parent_dir must be the parent of the directory itself
    # For files, parent_dir must be the directory containing the file
    parent_dir = os.path.dirname(source_path)
    log.info(f"Hashing pieces (this may take a while for large files)...")
    hash_start = time.time()
    lt.set_piece_hashes(t, parent_dir)
    hash_duration = time.time() - hash_start
    log.info(f"Piece hashing completed in {hash_duration:.1f}s")

    torrent_data = lt.bencode(t.generate())
    with open(torrent_path, "wb") as f:
        f.write(torrent_data)

    info = lt.torrent_info(torrent_path)
    magnet = lt.make_magnet_uri(info)

    log.info(f"Created torrent: {torrent_path} ({os.path.getsize(torrent_path):,} bytes)")
    log.info(f"Info hash: {info.info_hash()}")
    log.info(f"Magnet: {magnet[:120]}...")
    log.info(f"Pieces: {info.num_pieces()}, Piece size: {info.piece_length():,} bytes")
    log.info(f"Files in torrent: {info.num_files()}")

    # Log first few files
    for i in range(min(10, info.num_files())):
        fi = info.files()
        log.info(f"  [{i}] {fi.file_path(i)} ({fi.file_size(i):,} bytes)")
    if info.num_files() > 10:
        log.info(f"  ... and {info.num_files() - 10} more files")

    return torrent_path, magnet


def start_seeding(torrent_path: str, data_dir: str, listen_port: int = 6881):
    settings = {
        "listen_interfaces": f"0.0.0.0:{listen_port},[::]:{listen_port}",
        "enable_dht": True,
        "enable_lsd": True,
        "enable_upnp": False,
        "enable_natpmp": False,
        "alert_mask": (
            lt.alert.category_t.status_notification
            | lt.alert.category_t.error_notification
            | lt.alert.category_t.tracker_notification
            | lt.alert.category_t.dht_notification
        ),
    }
    ses = lt.session(settings)

    ses.add_dht_node(("router.bittorrent.com", 6881))
    ses.add_dht_node(("router.utorrent.com", 6881))
    ses.add_dht_node(("dht.transmissionbt.com", 6881))

    info = lt.torrent_info(torrent_path)
    handle = ses.add_torrent({
        "ti": info,
        "save_path": data_dir,
        "flags": lt.torrent_flags.seed_mode,
    })

    handle.force_reannounce()
    handle.force_dht_announce()

    log.info(f"Seeding started on port {listen_port}")
    log.info(f"Info hash: {info.info_hash()}")

    return ses, handle


def wait_for_seeding_ready(session, handle, timeout=120):
    start = time.time()
    while time.time() - start < timeout:
        status = handle.status()
        if status.is_seeding:
            log.info(
                f"Seeding confirmed. State: {status.state}, "
                f"Upload rate: {status.upload_rate / 1024:.1f} KB/s, "
                f"Peers: {status.num_peers}"
            )
            for alert in session.pop_alerts():
                log.debug(f"[{alert.what()}] {alert.message()}")
            return
        for alert in session.pop_alerts():
            log.debug(f"[{alert.what()}] {alert.message()}")
        time.sleep(1)
    raise TimeoutError(f"Torrent did not enter seeding state within {timeout}s")


def submit_to_torbox(api_key: str, torrent_path: str, name: str = None, magnet: str = None, retries: int = 3, allow_zip: bool = False) -> int:
    url = f"{TORBOX_BASE_URL}/v1/api/torrents/createtorrent"
    headers = {"Authorization": f"Bearer {api_key}"}

    last_error = None
    for attempt in range(1, retries + 1):
        log.info(f"Attempt {attempt}/{retries} — uploading .torrent file")
        with open(torrent_path, "rb") as f:
            files = {"file": (os.path.basename(torrent_path), f, "application/x-bittorrent")}
            data = {
                "seed": 1,
                "allow_zip": "true" if allow_zip else "false",
                "name": name or os.path.splitext(os.path.basename(torrent_path))[0],
            }
            response = requests.post(url, headers=headers, files=files, data=data, timeout=120)

        result = response.json()
        log.debug(f"TorBox response ({response.status_code}): {result}")

        if response.status_code == 200 and result.get("success"):
            torrent_id = int(result["data"]["torrent_id"])
            log.info(f"Submitted to TorBox. torrent_id={torrent_id}, hash={result['data'].get('hash')}")
            return torrent_id

        last_error = f"{result.get('error')} - {result.get('detail')}"
        log.warning(f"Attempt {attempt} failed: {last_error}")

        if attempt < retries:
            if magnet and attempt == 2:
                log.info(f"Attempt {attempt + 1}/{retries} — trying magnet link instead")
                data_magnet = {
                    "magnet": magnet,
                    "seed": 1,
                    "allow_zip": "true" if allow_zip else "false",
                    "name": name or "torbox_upload",
                }
                response = requests.post(url, headers=headers, data=data_magnet, timeout=120)
                result = response.json()
                log.debug(f"TorBox magnet response ({response.status_code}): {result}")

                if response.status_code == 200 and result.get("success"):
                    torrent_id = int(result["data"]["torrent_id"])
                    log.info(f"Submitted via magnet. torrent_id={torrent_id}")
                    return torrent_id

                last_error = f"{result.get('error')} - {result.get('detail')}"
                log.warning(f"Magnet attempt failed: {last_error}")

            log.info("Waiting 10s before retry...")
            time.sleep(10)

    raise RuntimeError(f"TorBox createtorrent failed after {retries} attempts: {last_error}")


def poll_torbox_status(api_key: str, torrent_id: int, timeout: int = 7200, interval: int = 10) -> dict:
    url = f"{TORBOX_BASE_URL}/v1/api/torrents/mylist"
    headers = {"Authorization": f"Bearer {api_key}"}
    terminal_failures = {"failed", "incomplete"}
    start = time.time()

    while time.time() - start < timeout:
        response = requests.get(
            url,
            headers=headers,
            params={"id": torrent_id, "bypass_cache": "true"},
            timeout=30,
        )
        response.raise_for_status()
        result = response.json()

        if not result.get("success"):
            raise RuntimeError(
                f"TorBox mylist error: {result.get('error')} - {result.get('detail')}"
            )

        torrent_data = result["data"]

        state = torrent_data.get("download_state", "unknown")
        finished = torrent_data.get("download_finished", False)
        progress = torrent_data.get("progress", 0)
        dl_speed = torrent_data.get("download_speed", 0)
        seeds = torrent_data.get("seeds", 0)
        peers = torrent_data.get("peers", 0)

        elapsed = time.time() - start
        log.info(
            f"[{elapsed:.0f}s] TorBox: state={state}, progress={progress:.1%}, "
            f"speed={dl_speed / 1024:.1f} KB/s, seeds={seeds}, peers={peers}, "
            f"finished={finished}"
        )

        if finished:
            log.info("TorBox download completed!")
            return torrent_data

        if state.lower() in terminal_failures:
            raise RuntimeError(f"TorBox download entered terminal failure state: {state}")

        time.sleep(interval)

    raise TimeoutError(f"TorBox download did not complete within {timeout}s")


def list_torrent_files(api_key: str, torrent_id: int) -> list:
    """List all files in a completed torrent on TorBox."""
    url = f"{TORBOX_BASE_URL}/v1/api/torrents/mylist"
    headers = {"Authorization": f"Bearer {api_key}"}

    response = requests.get(
        url, headers=headers,
        params={"id": torrent_id, "bypass_cache": "true"},
        timeout=30,
    )
    response.raise_for_status()
    result = response.json()

    if not result.get("success"):
        raise RuntimeError(f"TorBox error: {result.get('error')} - {result.get('detail')}")

    torrent_data = result["data"]
    files = torrent_data.get("files") or []

    log.info(f"Torrent: {torrent_data.get('name')} (id={torrent_id})")
    log.info(f"State: {torrent_data.get('download_state')}, Finished: {torrent_data.get('download_finished')}")
    log.info(f"Total size: {torrent_data.get('size', 0):,} bytes")
    log.info(f"Files ({len(files)}):")

    for f in files:
        size = f.get("size", 0)
        fid = f.get("id")
        name = f.get("name", f.get("short_name", "unknown"))
        size_mb = size / (1024 * 1024)
        log.info(f"  [file_id={fid}] {name} ({size_mb:.1f} MB)")

    return files


def request_download_link(api_key: str, torrent_id: int, file_id: int = None, zip_link: bool = False) -> str:
    """Request a download link from TorBox.

    Args:
        api_key: TorBox API key.
        torrent_id: The torrent ID.
        file_id: Specific file ID to download (None = whole torrent).
        zip_link: If True, get a zip of the whole torrent.
    """
    url = f"{TORBOX_BASE_URL}/v1/api/torrents/requestdl"
    params = {
        "token": api_key,
        "torrent_id": torrent_id,
        "zip_link": str(zip_link).lower(),
    }
    if file_id is not None:
        params["file_id"] = file_id

    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()
    result = response.json()

    if not result.get("success"):
        raise RuntimeError(
            f"TorBox requestdl error: {result.get('error')} - {result.get('detail')}"
        )

    download_url = result["data"]
    log.info(f"Got download link: {download_url[:80]}...")
    return download_url


def download_file(url: str, output_path: str) -> str:
    log.info(f"Downloading to {output_path}...")
    response = requests.get(url, stream=True, timeout=300)
    response.raise_for_status()

    total = int(response.headers.get("content-length", 0))
    downloaded = 0
    last_log = 0

    with open(output_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=65536):
            f.write(chunk)
            downloaded += len(chunk)
            # Log every ~5MB
            if total > 0 and downloaded - last_log >= 5 * 1024 * 1024:
                pct = downloaded / total * 100
                log.info(f"Download progress: {pct:.1f}% ({downloaded:,}/{total:,} bytes)")
                last_log = downloaded

    actual_size = os.path.getsize(output_path)
    log.info(f"Download complete: {output_path} ({actual_size:,} bytes)")
    return output_path


def cleanup(session, handle, temp_dir):
    log.info("Cleaning up...")

    if handle and session:
        try:
            handle.pause()
            session.remove_torrent(handle)
            time.sleep(1)
            del session
            log.info("Seeding stopped and session destroyed.")
        except Exception as e:
            log.warning(f"Error during seeding cleanup: {e}")

    if temp_dir and os.path.exists(temp_dir):
        shutil.rmtree(temp_dir, ignore_errors=True)
        log.info(f"Removed temp directory: {temp_dir}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="TorBox E2E Torrent Seed & Download",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Mode selection
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--source-dir", help="Seed an existing directory to TorBox")
    mode.add_argument("--list-files", type=int, metavar="TORRENT_ID",
                       help="List files in a completed torrent on TorBox")
    mode.add_argument("--download", type=int, metavar="TORRENT_ID",
                       help="Download from a completed torrent on TorBox")

    # Common options
    parser.add_argument("--api-key", default=os.environ.get("TORBOX_API_KEY"),
                        help="TorBox API key (or set TORBOX_API_KEY env var)")
    parser.add_argument("--port", type=int, default=LISTEN_PORT,
                        help="Seeding port (default: 6881)")
    parser.add_argument("--output-dir", default=".",
                        help="Directory for downloaded files (default: .)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable debug logging")

    # Test mode options
    parser.add_argument("--size-mb", type=int, default=DUMMY_SIZE_MB,
                        help="Dummy zip size in MB for test mode (default: 3)")

    # Directory mode options
    parser.add_argument("--seed-only", action="store_true",
                        help="Only create torrent and seed, don't submit to TorBox")
    parser.add_argument("--name", help="Name for the torrent on TorBox")
    parser.add_argument("--torrent-out", help="Save the .torrent file to this path")
    parser.add_argument("--allow-zip", action="store_true",
                        help="Allow TorBox to zip files (default: no zip, individual files)")

    # Polling options
    parser.add_argument("--poll-interval", type=int, default=POLL_INTERVAL,
                        help="Seconds between status polls (default: 10)")
    parser.add_argument("--poll-timeout", type=int, default=POLL_TIMEOUT,
                        help="Max seconds to wait for TorBox download (default: 7200)")

    # Download mode options
    parser.add_argument("--file-id", type=int, help="Download a specific file by ID")
    parser.add_argument("--zip", action="store_true",
                        help="Download as zip (for --download mode)")

    parser.add_argument("--keep-temp", action="store_true",
                        help="Don't delete temp files on exit")

    args = parser.parse_args()

    if not args.api_key:
        parser.error("API key required: use --api-key or set TORBOX_API_KEY env var")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    # ── List files mode ──
    if args.list_files:
        files = list_torrent_files(args.api_key, args.list_files)
        print(json.dumps(files, indent=2))
        return

    # ── Download mode ──
    if args.download:
        log.info(f"Requesting download for torrent_id={args.download}" +
                 (f", file_id={args.file_id}" if args.file_id else "") +
                 (", as zip" if args.zip else ""))
        download_url = request_download_link(
            args.api_key, args.download,
            file_id=args.file_id, zip_link=args.zip,
        )
        # Determine output filename from URL or use default
        output_name = f"torbox_download_{args.download}"
        if args.file_id:
            output_name += f"_file{args.file_id}"
        output_path = os.path.join(args.output_dir, output_name)
        download_file(download_url, output_path)
        return

    # ── Seed mode (test or directory) ──
    temp_dir = None
    session = None
    handle = None

    if not args.source_dir:
        temp_dir = tempfile.mkdtemp(prefix="torbox_e2e_")

    def signal_handler(sig, frame):
        log.info(f"Received signal {sig}, shutting down...")
        cleanup(session, handle, temp_dir if temp_dir and not args.keep_temp else None)
        sys.exit(1)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        # Step 0: Public IP
        try:
            public_ip = get_public_ip()
            log.info(f"Public IP: {public_ip}")
        except Exception:
            log.warning("Could not determine public IP; continuing anyway")

        # Step 1: Determine source path
        if args.source_dir:
            # Directory mode — seed existing files
            source_path = os.path.abspath(args.source_dir)
            if not os.path.exists(source_path):
                parser.error(f"Source directory not found: {source_path}")
            data_dir = os.path.dirname(source_path)
            torrent_name = args.name or os.path.basename(source_path)

            log.info("=" * 60)
            log.info(f"DIRECTORY MODE: Seeding {source_path}")
            log.info("=" * 60)

            # Count files and total size
            total_size = 0
            file_count = 0
            for root, dirs, files in os.walk(source_path):
                for fname in files:
                    fpath = os.path.join(root, fname)
                    total_size += os.path.getsize(fpath)
                    file_count += 1
            log.info(f"Source: {file_count} files, {total_size:,} bytes ({total_size / (1024**3):.2f} GB)")
        else:
            # Test mode — create dummy zip
            log.info("=" * 60)
            log.info("TEST MODE: Creating dummy zip file")
            log.info("=" * 60)
            source_path = os.path.join(temp_dir, "test_payload.zip")
            create_dummy_zip(source_path, args.size_mb)
            data_dir = temp_dir
            torrent_name = "torbox_e2e_test"

        # Step 2: Create .torrent
        log.info("=" * 60)
        log.info("Creating .torrent file")
        log.info("=" * 60)
        if args.torrent_out:
            torrent_path = os.path.abspath(args.torrent_out)
        elif temp_dir:
            torrent_path = os.path.join(temp_dir, f"{torrent_name}.torrent")
        else:
            torrent_path = os.path.join(os.path.dirname(source_path), f"{torrent_name}.torrent")

        torrent_path, magnet = create_torrent_file(source_path, torrent_path, PUBLIC_TRACKERS)

        if args.torrent_out:
            log.info(f"Torrent file saved to: {torrent_path}")

        # Step 3: Start seeding
        log.info("=" * 60)
        log.info("Starting seeder")
        log.info("=" * 60)
        session, handle = start_seeding(torrent_path, data_dir, args.port)
        wait_for_seeding_ready(session, handle, timeout=120)

        if args.seed_only:
            log.info("=" * 60)
            log.info("SEED-ONLY MODE: Seeding until interrupted (Ctrl+C to stop)")
            log.info(f"Torrent file: {torrent_path}")
            log.info(f"Magnet: {magnet[:120]}...")
            log.info("=" * 60)
            # Seed indefinitely, logging status periodically
            while True:
                status = handle.status()
                log.info(
                    f"Seeding: upload={status.upload_rate / 1024:.1f} KB/s, "
                    f"total_upload={status.total_upload / (1024*1024):.1f} MB, "
                    f"peers={status.num_peers}"
                )
                for alert in session.pop_alerts():
                    log.debug(f"[{alert.what()}] {alert.message()}")
                time.sleep(10)

        # Step 4: Submit to TorBox
        log.info("=" * 60)
        log.info("Submitting torrent to TorBox")
        log.info("=" * 60)
        torrent_id = submit_to_torbox(args.api_key, torrent_path, name=torrent_name, magnet=magnet, allow_zip=args.allow_zip)

        # Step 5: Poll until done
        log.info("=" * 60)
        log.info("Waiting for TorBox to download from us")
        log.info("=" * 60)
        torrent_data = poll_torbox_status(
            args.api_key, torrent_id, args.poll_timeout, args.poll_interval
        )

        # Step 6: List files
        log.info("=" * 60)
        log.info(f"TorBox download complete! torrent_id={torrent_id}")
        log.info("=" * 60)
        files = torrent_data.get("files") or []
        log.info(f"Files available ({len(files)}):")
        for f in files:
            size = f.get("size", 0)
            fid = f.get("id")
            name = f.get("name", f.get("short_name", "unknown"))
            log.info(f"  [file_id={fid}] {name} ({size / (1024*1024):.1f} MB)")

        log.info("")
        log.info("To download individual files later:")
        log.info(f"  ./venv/bin/python3 torbox_e2e.py --api-key <KEY> --download {torrent_id} --file-id <FILE_ID>")
        log.info("")
        log.info("To download everything as a zip:")
        log.info(f"  ./venv/bin/python3 torbox_e2e.py --api-key <KEY> --download {torrent_id} --zip")

        # For test mode, also do the full download-back verification
        if not args.source_dir:
            log.info("=" * 60)
            log.info("Downloading file back from TorBox CDN (test verification)")
            log.info("=" * 60)
            download_url = request_download_link(args.api_key, torrent_id)
            output_filename = torrent_data.get("name", "downloaded_file")
            output_path = os.path.join(args.output_dir, output_filename)
            download_file(download_url, output_path)

            original_size = os.path.getsize(source_path)
            downloaded_size = os.path.getsize(output_path)
            log.info(f"Original size:   {original_size:,} bytes")
            log.info(f"Downloaded size: {downloaded_size:,} bytes")
            if original_size == downloaded_size:
                log.info("SUCCESS: File sizes match!")
            else:
                log.warning(f"WARNING: Size mismatch! original={original_size:,}, downloaded={downloaded_size:,}")

    except KeyboardInterrupt:
        log.info("Interrupted by user.")
    except Exception as e:
        log.error(f"Fatal error: {e}", exc_info=True)
        raise
    finally:
        cleanup(session, handle, temp_dir if temp_dir and not args.keep_temp else None)


if __name__ == "__main__":
    main()
