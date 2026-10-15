# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.2] - 2026-10-15

### Fixed
- Prevented FFmpeg MP3 frame padding at exact 5-minute boundaries from creating a tiny extra trailing chunk that Groq could reject with HTTP 400.
- Long-media chunk planning now allows the final chunk to absorb up to 30 extra seconds (maximum 5m30s), while longer remainders are split normally.
- Groq quota preflight and per-chunk accounting now use the same deterministic chunk plan used for transcription.

## [2.0.1] - 2026-10-11

### Added
- `/language [code|auto]` command to configure or force transcription language (`auto`, `ta`, `en`, etc.).
- Persistent language configuration stored in `data/selected_language.txt` surviving restarts.
- Support for forcing Tamil (`/language ta`) to prevent Whisper from translating phone conversations with English tech jargon into English.
- `/version` command to view the running bot version and release changelog.
- `CHANGELOG.md` to track version history and release notes.

## [2.0.0] - 2026-09-20

### Added
- Zero-inbound automated pull-based CI/CD deployment pipeline.
- GitHub Actions CI for automated compilation and unit testing.
- Server-side systemd timer and atomic deployment script with automatic rollback.
- Unit test suite covering core behavior, bot helper functions, and usage tracking.

## [1.0.0] - 2026-08-23

### Added
- Initial release of Private Groq-Powered Telegram Transcription Bot.
- Voice note, audio, and video transcription using Groq's `whisper-large-v3-turbo`.
- Automatic rate-limit tracking across Groq's 4 rolling limits (`/usage`, `/usage_detailed`).
- Chunked transcription for media longer than five minutes with merged `.txt` document download.
- Local Telegram Bot API server integration for media larger than 20 MB.
