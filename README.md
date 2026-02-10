# 🎙️ WhisperKey

[![PyPI version](https://img.shields.io/pypi/v/whisperkey.svg)](https://pypi.org/project/whisperkey/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python Version](https://img.shields.io/pypi/pyversions/whisperkey)](https://pypi.org/project/whisperkey/)

**WhisperKey** is a lightweight application that lets you transcribe speech to text. Simply press a keyboard shortcut, speak, and get your transcription copied directly to your clipboard.

## ✨ Features

- 🔑 **Global Hotkeys**: Start/stop standard recording with Ctrl+Alt+G or cleaned (LLM-processed) recording with Ctrl+Alt+F from anywhere on your system
- 📋 **Clipboard Integration**: Automatically copies transcriptions to your clipboard
- 🔒 **Privacy-Focused**: Audio recordings are stored temporarily in your local cache

## 🚀 Installation

### Prerequisites

- Python 3.12 or higher
- OpenAI API key

### Using pip

```bash
pip install whisperkey
```

### From source

```bash
git clone https://github.com/Danielratmiroff/whisper-key.git
cd whisper-key
uv sync  # Install uv first: curl -LsSf https://astral.sh/uv/install.sh | sh
```

## ⚙️ Configuration

Before using WhisperKey, you need to set up your OpenAI API key:

```bash
export OPENAI_API_KEY="your-api-key-here"
```

For permanent configuration, add this to your shell profile file (`.bashrc`, `.zshrc`, etc.).

## 🎮 Usage

1. Start WhisperKey:
   ```bash
   whisperkey
   ```

2. Press **Ctrl+Alt+G** to start a standard recording, or **Ctrl+Alt+F** to start a cleaned (LLM-processed) recording

3. Press the same hotkey again (**Ctrl+Alt+G** or **Ctrl+Alt+F**) to stop recording

4. The transcription will be processed and automatically copied to your clipboard

## 🐧 Input Device Permissions

WhisperKey uses `evdev` to read keyboard input directly from `/dev/input/`. The current user must be in the `input` group:

```bash
sudo usermod -aG input $USER
```

Log out and back in for the group change to take effect.

### Running inside LXD

When running WhisperKey inside an LXD container, you need to share the host's keyboard input device into the container.

1. **Find your keyboard device on the host:**

   ```bash
   # On the host
   for f in /dev/input/event*; do
     name=$(cat /sys/class/input/$(basename $f)/device/name 2>/dev/null)
     echo "$f: $name"
   done
   ```

   Look for the entry that corresponds to your keyboard (e.g. `/dev/input/event3`).

2. **Find the `input` group GID inside the container:**

   ```bash
   lxc exec <container> -- getent group input
   ```

   Note the GID (e.g. `995`).

3. **Share the keyboard device into the container:**

   ```bash
   lxc config device add <container> kbd-input unix-char \
     source=/dev/input/event3 \
     path=/dev/input/event3 \
     gid=995 \
     mode=0660
   ```

   Replace `event3` with your actual keyboard device and `995` with the GID from step 2.

4. **Ensure the user inside the container is in the `input` group** (see above).

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add some amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request from your forked repository

## 📄 License

This project is licensed under the MIT License - see the LICENSE file for details.

## 🙏 Acknowledgements

- [OpenAI Whisper](https://openai.com/research/whisper) for the speech recognition API
- [PyAudio](https://people.csail.mit.edu/hubert/pyaudio/) for audio recording capabilities
- [evdev](https://pypi.org/project/evdev/) for keyboard input handling
