#!/usr/bin/env python3
"""
Sync your Spotify Liked Songs into a public playlist.

SETUP
1. pip install spotipy
2. Create an app at https://developer.spotify.com/dashboard
   - Add redirect URI: http://127.0.0.1:8888/callback
3. Set env vars (or put them in your cron/CI environment):
     export SPOTIPY_CLIENT_ID=...
     export SPOTIPY_CLIENT_SECRET=...
     export SPOTIPY_REDIRECT_URI=http://127.0.0.1:8888/callback
4. Run once by hand: python sync_liked_songs.py
   (a browser opens for login; the token is cached in .spotify_cache
    and refreshed automatically after that)

USAGE
  python sync_liked_songs.py                    # add new likes
  python sync_liked_songs.py --prune            # also remove songs you've unliked
  python sync_liked_songs.py --name "My Likes"  # custom playlist name

SCHEDULE (cron, every 6 hours)
  0 */6 * * * cd /path/to/script && /usr/bin/python3 sync_liked_songs.py >> sync.log 2>&1
"""

import argparse
import os
import sys

import spotipy
from spotipy.oauth2 import SpotifyOAuth

SCOPES = " ".join([
    "user-library-read",
    "playlist-modify-public",
    "playlist-modify-private",
    "playlist-read-private",
])
CACHE_PATH = ".spotify_cache"
BATCH = 100  # Spotify's max tracks per add/remove request


def get_client() -> spotipy.Spotify:
    auth = SpotifyOAuth(scope=SCOPES, cache_path=CACHE_PATH, open_browser=True)

    # Headless mode (GitHub Actions): trade a stored refresh token for an access token.
    refresh_token = os.environ.get("SPOTIFY_REFRESH_TOKEN")
    if refresh_token:
        token_info = auth.refresh_access_token(refresh_token)
        return spotipy.Spotify(auth=token_info["access_token"])

    # Local mode: interactive login the first time, cached after that.
    return spotipy.Spotify(auth_manager=auth)


def get_liked_uris(sp) -> list[str]:
    """All liked song URIs, newest first (Spotify's order)."""
    uris, offset = [], 0
    while True:
        page = sp.current_user_saved_tracks(limit=50, offset=offset)
        for item in page["items"]:
            track = item.get("track")
            if track and track.get("uri"):
                uris.append(track["uri"])
        if not page["next"]:
            break
        offset += 50
    return uris


def get_playlist_uris(sp, playlist_id: str) -> list[str]:
    uris, offset = [], 0
    while True:
        # Spotify's Feb 2026 API: /tracks became /items and "track" became "item"
        page = sp._get(f"playlists/{playlist_id}/items", limit=100, offset=offset)
        for entry in page["items"]:
            track = entry.get("item") or entry.get("track")
            if track and track.get("uri"):
                uris.append(track["uri"])
        if not page["next"]:
            break
        offset += 100
    return uris


def find_or_create_playlist(sp, name: str) -> str:
    me = sp.current_user()["id"]
    offset = 0
    while True:
        page = sp.current_user_playlists(limit=50, offset=offset)
        for pl in page["items"]:
            if pl and pl["name"] == name and pl["owner"]["id"] == me:
                # make sure it's still public
                if not pl.get("public"):
                    sp.playlist_change_details(pl["id"], public=True)
                return pl["id"]
        if not page["next"]:
            break
        offset += 50

    # POST /users/{id}/playlists was removed in Feb 2026; /me/playlists replaces it
    pl = sp._post("me/playlists", payload={
        "name": name,
        "public": True,
        "description": "Auto-synced from my Liked Songs.",
    })
    print(f"Created playlist '{name}'")
    return pl["id"]


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="Liked Songs (Public)")
    parser.add_argument("--prune", action="store_true",
                        help="remove tracks from the playlist that you've unliked")
    parser.add_argument("--playlist-id", default=os.environ.get("PLAYLIST_ID"),
                        help="use an existing playlist (skips find/create); "
                             "also settable via the PLAYLIST_ID env var")
    args = parser.parse_args()

    sp = get_client()
    if args.playlist_id:
        playlist_id = args.playlist_id
    else:
        playlist_id = find_or_create_playlist(sp, args.name)

    liked = get_liked_uris(sp)
    in_playlist = get_playlist_uris(sp, playlist_id)
    in_playlist_set = set(in_playlist)
    liked_set = set(liked)

    # New likes, still newest-first. Dedupe while preserving order.
    to_add = list(dict.fromkeys(u for u in liked if u not in in_playlist_set))

    # Insert at the top so newest likes appear first, like the real Liked Songs.
    # Go from the oldest chunk to the newest so the final order is correct.
    batches = list(chunks(to_add, BATCH))
    for batch in reversed(batches):
        sp._post(f"playlists/{playlist_id}/items",
                 payload={"uris": batch, "position": 0})
    print(f"Added {len(to_add)} new track(s)")

    if args.prune:
        to_remove = list(dict.fromkeys(u for u in in_playlist if u not in liked_set))
        for batch in chunks(to_remove, BATCH):
            sp._delete(f"playlists/{playlist_id}/items",
                       payload={"items": [{"uri": u} for u in batch]})
        print(f"Removed {len(to_remove)} unliked track(s)")

    print(f"Done. {len(liked)} liked songs total.")


if __name__ == "__main__":
    try:
        main()
    except spotipy.SpotifyException as e:
        print(f"Spotify API error: {e}", file=sys.stderr)
        sys.exit(1)
