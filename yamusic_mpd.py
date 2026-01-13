#!/usr/bin/env python3
import hashlib
import logging
import os
import time
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree

import requests
from mpd import MPDClient

# --- CONFIGURATION ---
API_BASE_URL = "https://api.music.yandex.net"
CLIENT_ID = "WindowsPhone/3.20"
SIGN_SALT = "XGRlBW9FXlekgbPrRHuSiA"
# Use environment variable for music directory or fallback to default
DEFAULT_MUSIC_DIR = os.getenv("YAMUSIC_DIR", "~/Music/yandex-music")

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


class YandexMusicMPD:
    def __init__(self, token: str):
        if not token or token == "YOUR_TOKEN_HERE":
            raise ValueError("Oauth token is missing! Set YAMUSIC_TOKEN env var.")

        self.token = token
        self.headers = {
            "Authorization": f"OAuth {token}",
            "X-Yandex-Music-Client": CLIENT_ID,
        }
        self.music_dir = os.path.expanduser(DEFAULT_MUSIC_DIR)
        os.makedirs(self.music_dir, exist_ok=True)
        self.client = MPDClient()

    def __enter__(self):
        self._connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._disconnect()

    def _connect(self, host: str = "localhost", port: int = 6600):
        """Establish connection with the MPD daemon."""
        try:
            self.client.timeout = 10
            self.client.connect(host, port)
            logger.info(f"Connected to MPD at {host}:{port}")
        except Exception as e:
            logger.warning(f"Could not connect to MPD: {e}")

    def _disconnect(self):
        """Safely close MPD connection."""
        try:
            self.client.close()
            self.client.disconnect()
        except Exception:
            pass

    def get_account_uid(self) -> str:
        """Fetch Yandex Music account UID."""
        res = requests.get(f"{API_BASE_URL}/account/status", headers=self.headers)
        res.raise_for_status()
        data = res.json()["result"]
        logger.info(f"Logged in as: {data['account']['login']}")
        return str(data["account"]["uid"])

    def get_liked_tracks(self, user_id: str, limit: int) -> List[Dict[str, Any]]:
        """Fetch detailed info for liked tracks."""
        logger.info("Fetching library...")
        url = f"{API_BASE_URL}/users/{user_id}/likes/tracks"
        res = requests.get(url, headers=self.headers)
        res.raise_for_status()

        likes = res.json()["result"]["library"]["tracks"][:limit]
        if not likes:
            return []

        track_ids = [
            f"{t['id']}:{t.get('albumId', '')}" if t.get("albumId") else str(t["id"])
            for t in likes
        ]

        res = requests.post(
            f"{API_BASE_URL}/tracks",
            headers=self.headers,
            params={"track-ids": ",".join(track_ids)},
        )
        res.raise_for_status()
        return res.json()["result"]

    def _get_direct_link(self, track_id: str) -> str:
        """Generate a direct download URL for a track."""
        info_url = f"{API_BASE_URL}/tracks/{track_id}/download-info"
        res = requests.get(info_url, headers=self.headers).json()

        suitable = next(
            (
                i
                for i in res["result"]
                if i["codec"] == "mp3" and i["bitrateInKbps"] == 320
            ),
            res["result"][0],
        )

        xml_res = requests.get(suitable["downloadInfoUrl"], headers=self.headers)
        xml_data = ElementTree.fromstring(xml_res.text)
        fields = {node.tag: node.text for node in xml_data}

        path_norm = fields["path"][1:]
        sign_str = f"{SIGN_SALT}{path_norm}{fields['s']}"
        sign = hashlib.md5(sign_str.encode()).hexdigest()

        return f"https://{fields['host']}/get-mp3/{sign}/{fields['ts']}/{path_norm}"

    def _write_tags(
        self, path: str, title: str, artist: str, album: str, cover_url: Optional[str]
    ):
        """Write ID3v2.3 tags for compatibility with most MPD clients."""
        try:
            from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1, ID3NoHeaderError

            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                tags = ID3()

            tags.delete(path)
            tags.add(TIT2(encoding=3, text=title))
            tags.add(TPE1(encoding=3, text=artist))
            tags.add(TALB(encoding=3, text=album))

            if cover_url:
                try:
                    img_url = (
                        "https://" + cover_url.replace("%%", "600x600")
                        if "%%" in cover_url
                        else "https://" + cover_url
                    )
                    img_data = requests.get(img_url, timeout=10).content
                    tags.add(
                        APIC(
                            encoding=3,
                            mime="image/jpeg",
                            type=3,
                            desc="Cover",
                            data=img_data,
                        )
                    )
                except Exception as e:
                    logger.debug(f"Cover download failed: {e}")

            tags.save(path, v2_version=3)
        except ImportError:
            logger.error("Library 'mutagen' is missing. Run: pip install mutagen")
        except Exception as e:
            logger.error(f"Failed to write tags: {e}")

    def sync_track(self, track: Dict[str, Any]) -> Optional[str]:
        """Download track and write metadata if not exists."""
        artists = ", ".join(a["name"] for a in track.get("artists", []))
        title = track.get("title", "Unknown")
        album = (
            track["albums"][0].get("title", "Unknown Album")
            if track.get("albums")
            else "Unknown Album"
        )
        cover_url = track.get("coverUri") or track.get("ogImage")

        filename = f"{artists} - {title}".replace("/", "_").replace(":", "_") + ".mp3"
        local_path = os.path.join(self.music_dir, filename)

        if os.path.exists(local_path):
            return filename

        try:
            logger.info(f"Downloading: {artists} - {title}")
            link = self._get_direct_link(str(track["id"]))
            content = requests.get(link).content

            with open(local_path, "wb") as f:
                f.write(content)

            self._write_tags(local_path, title, artists, album, cover_url)
            return filename
        except Exception as e:
            logger.error(f"Failed to sync track {title}: {e}")
            if os.path.exists(local_path):
                os.remove(local_path)
            return None

    def sync_and_play(self, limit: int = 100):
        """Synchronize liked tracks and populate MPD playlist."""
        uid = self.get_account_uid()
        tracks = self.get_liked_tracks(uid, limit)

        if not tracks:
            logger.warning("No tracks found.")
            return

        logger.info(f"Processing {len(tracks)} tracks...")
        filenames = []
        for i, track in enumerate(tracks, 1):
            print(f"[{i}/{len(tracks)}]", end="\r")
            fname = self.sync_track(track)
            if fname:
                filenames.append(fname)

        print("\nSync complete.")

        try:
            self.client.ping()
            self.client.update()
            time.sleep(2)
            self.client.clear()

            added = 0
            for fname in filenames:
                try:
                    self.client.add(f"yandex-music/{fname}")
                    added += 1
                except:
                    try:
                        self.client.add(fname)
                        added += 1
                    except:
                        continue

            if added > 0:
                self.client.play(0)
                logger.info(f"Playback started! Added {added} tracks.")
            else:
                logger.error(
                    "Could not add tracks to MPD. Check music_directory in mpd.conf."
                )
        except Exception as e:
            logger.error(f"MPD interaction error: {e}")


def main():
    # Security: prioritized environment variable over hardcoded string
    TOKEN = os.getenv("YAMUSIC_TOKEN", "YOUR_TOKEN_HERE")

    app = YandexMusicMPD(TOKEN)
    try:
        print("1. Sync & Play Likes")
        choice = input("Choice: ").strip()

        if choice == "1":
            limit_in = input("Track limit (Default 100): ").strip()
            limit = int(limit_in) if limit_in.isdigit() else 100
            app.sync_and_play(limit)
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
    except Exception as e:
        logger.critical(f"Fatal error: {e}")


if __name__ == "__main__":
    main()
