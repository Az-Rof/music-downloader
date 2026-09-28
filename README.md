# Music Downloader

A **local** web application for downloading music from **Spotify** & **YouTube Music**
as **MP3** files. Simply paste a song, playlist, or album link — the app
downloads everything with full metadata.

## Features

- **Two sources**: Spotify links (songs / playlists / albums / artists) & YouTube /
  YouTube Music (songs / playlists / albums)
- **MP3 + full metadata**: title, artist, album, year, track number, and
  cover art automatically embedded in the file
- **Quality options**: automatic (follow source) or 128 / 192 / 256 / 320 kbps
- **Duplicate skip**: songs that have already been downloaded are automatically skipped
- **Automatic folder organization**: `downloads/<Artist>/<Album>/<Title>.mp3`
- **Real-time progress** for metadata reading and downloading, visible directly in the browser
- **Automatic FFmpeg** downloaded on first run if not already installed

## Getting Started

```bash
# 1. Make sure Python 3.10+ is installed
# 2. Install dependencies
pip install -r requirements.txt

# 3. Run the application
python app.py
```

Then open **http://127.0.0.1:5000** in your browser.

## Screenshots

### Main Interface
![Main Interface](screenshoot/Screenshot%202026-09-28%20175118.png)

### Download in Progress
![Download in Progress](screenshoot/Screenshot%202026-09-28%20175636.png)

### Downloaded File
![Downloaded File](screenshoot/Screenshot%202026-09-28%20181137.png)

## �📁 Project Structure

```
Music Downloader/
├── app.py              # Flask web server (API + page)
├── downloader.py       # Download engine (worker thread + spotdl)
├── templates/
│   └── index.html      # Web interface
├── downloads/          # Download output (auto-created)
└── requirements.txt
```

## Notes & Troubleshooting

- **Source quality**: audio on YouTube (Music) maxes out at ~128 kbps (256 kbps
  for Premium accounts). Selecting 320 kbps will re-encode to 320 kbps, but
  quality cannot exceed the source — choose "Automatic" for the most efficient result.
- **"No results found"**: the song is not available on YouTube (e.g. exclusive to
  a platform), or the playlist link is private.
- **FFmpeg**: if auto-download fails, install it manually from
  https://ffmpeg.org/download.html then restart the application.
- **`youtube.com` / `youtu.be` links** are automatically converted to `music.youtube.com`.
- **Very large playlists** (hundreds of songs) take a long time — keep the tab
  open, progress is shown per song.

## Legal

This tool fetches audio from YouTube with metadata from Spotify (same behavior
as the open-source `spotdl` project). Use only for copyright-free content or
content you are licensed to use. You are responsible for your usage.
